"""
Runs and jobs whose completion never arrived. GitHub delivers webhooks at most once and sometimes not at all: a job's
`workflow_job completed` can go missing while its run's `workflow_run completed` arrives, and the job then shows as
running forever on the fleet pages. A run's own completion can go missing too, and then it also holds its batch
project's next slot. Whenever a run completes, and once at start, control first reads from the API every run left
unfinished with no unfinished job, then the jobs of every completed run that still holds an unfinished one. Neither
upsert moves a status backwards, so a late webhook and the API answer can arrive in either order.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from typing import Any

import httpx

from .github import GitHub, installation_client
from .model import Job, Run, is_build_job
from .state import State

log = logging.getLogger(__name__)

HEAL_DELAY_SECONDS = 30.0
"""Job webhooks can trail their run's by a few seconds; give them that long before asking the API."""

RUN_QUIET = datetime.timedelta(minutes=5)
"""How long a run with no unfinished job may sit unfinished before control asks the API: its jobs' webhooks and its
own completion normally follow within seconds."""

RUN_WINDOW = datetime.timedelta(days=2)
"""Older runs are left alone, so a run deleted on GitHub is not asked about forever."""


async def heal(state: State, delay: float = HEAL_DELAY_SECONDS) -> int:
    """Re-reads every unfinished run with no unfinished job, then the jobs of every completed run with an unfinished
    job. Returns how many runs and jobs it corrected."""
    if delay:
        await asyncio.sleep(delay)
    now = datetime.datetime.now(datetime.UTC)
    unfinished = state.model.runs_left_unfinished(
        (now - RUN_WINDOW).isoformat(timespec="seconds"), (now - RUN_QUIET).isoformat(timespec="seconds")
    )
    if not unfinished and not state.model.runs_with_unfinished_jobs():
        return 0
    github = await installation_client(state)
    if github is None:
        return 0
    fixed = 0
    names: dict[str, dict[Any, str]] = {}
    for repo, run_id in unfinished:
        try:
            fixed += await read_run(state, github, repo, run_id, names)
        except Exception:  # as below: the next completed run tries again
            log.exception("reading %s run %s failed", repo, run_id)
    for repo, run_id in state.model.runs_with_unfinished_jobs():
        try:
            fixed += await read_jobs(state, github, repo, run_id)
        except Exception:  # one run's failure must not stop the others; the next completed run tries again
            log.exception("reading the jobs of %s run %s failed", repo, run_id)
    if fixed:
        log.info("corrected %s runs and jobs whose completion webhook never arrived", fixed)
    return fixed


async def read_run(state: State, github: GitHub, repo: str, run_id: int, names: dict[str, dict[Any, str]]) -> bool:
    """Store a run as the API has it, under its workflow's own name (`names` caches each repo's workflows; a run's
    `name` may be its run-name). A run GitHub no longer has is dropped with its jobs. True when its status changed."""
    before = state.model.run(run_id)
    try:
        data = (await github.get(f"/repos/{repo}/actions/runs/{run_id}")).json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise
        state.model.delete_run(run_id)
        state.stream.publish("runs", run_id)
        return before is not None
    if repo not in names:
        workflows = await github.paginate(f"/repos/{repo}/actions/workflows", {"per_page": 100}, key="workflows")
        names[repo] = {w["id"]: str(w["name"]) for w in workflows if w.get("id") and w.get("name")}
    run = Run.from_github(repo, data, names[repo].get(data.get("workflow_id")))
    state.model.upsert_run(run)
    after = state.model.run(run_id)
    changed = after is not None and (
        before is None or (before.status, before.conclusion) != (after.status, after.conclusion)
    )
    if changed:
        state.stream.publish("runs", run_id)
    return changed


async def read_jobs(state: State, github: GitHub, repo: str, run_id: int) -> int:
    """Store a run's jobs as the API has them. Returns how many changed status."""
    jobs = await github.paginate(f"/repos/{repo}/actions/runs/{run_id}/jobs", {"per_page": 100}, key="jobs", limit=200)
    changed = 0
    for data in jobs:
        if not is_build_job(data):
            continue
        before = state.model.job(int(data["id"]))
        state.model.upsert_job(Job.from_github(repo, data))
        after = state.model.job(int(data["id"]))
        if after and (before is None or (before.status, before.conclusion) != (after.status, after.conclusion)):
            changed += 1
            state.stream.publish("jobs", data["id"])
    state.stream.publish("runs", run_id)
    return changed


def schedule_heal(state: State, payload: dict[str, Any]) -> asyncio.Task[Any] | None:
    """Fire-and-forget from the webhook handler, for every completed run."""
    if payload.get("action") != "completed":
        return None
    return state.spawn(heal(state))
