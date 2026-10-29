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

from .github import installation_client
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
    for repo, run_id in unfinished:
        try:
            data = (await github.get(f"/repos/{repo}/actions/runs/{run_id}")).json()
        except Exception:  # as below: the next completed run tries again
            log.exception("reading %s run %s failed", repo, run_id)
            continue
        if state.model.upsert_run(Run.from_github(repo, data)) and data.get("status") == "completed":
            fixed += 1
            state.stream.publish("runs", run_id)
    runs = state.model.runs_with_unfinished_jobs()
    for repo, run_id in runs:
        try:
            jobs = await github.paginate(
                f"/repos/{repo}/actions/runs/{run_id}/jobs", {"per_page": 100}, key="jobs", limit=200
            )
        except Exception:  # one run's failure must not stop the others; the next completed run tries again
            log.exception("reading the jobs of %s run %s failed", repo, run_id)
            continue
        for data in jobs:
            if is_build_job(data) and state.model.upsert_job(Job.from_github(repo, data)):
                fixed += 1
                state.stream.publish("jobs", data["id"])
        state.stream.publish("runs", run_id)
    if fixed:
        log.info("corrected %s runs and jobs whose completion webhook never arrived", fixed)
    return fixed


def schedule_heal(state: State, payload: dict[str, Any]) -> asyncio.Task[Any] | None:
    """Fire-and-forget from the webhook handler, for every completed run."""
    if payload.get("action") != "completed":
        return None
    return state.spawn(heal(state))
