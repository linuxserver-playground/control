"""
Jobs whose completion never arrived. GitHub delivers webhooks at most once and sometimes not at all: a job's
`workflow_job completed` can go missing while its run's `workflow_run completed` arrives, and the job then shows as
running forever on the fleet pages. Whenever a run completes, and once at start, control looks for completed runs
that still hold an unfinished job and reads those runs' jobs from the API. The job upsert never moves a status
backwards, so a late webhook and the API answer can arrive in either order.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .github import installation_client
from .model import Job, is_build_job
from .state import State

log = logging.getLogger(__name__)

HEAL_DELAY_SECONDS = 30.0
"""Job webhooks can trail their run's by a few seconds; give them that long before asking the API."""


async def heal(state: State, delay: float = HEAL_DELAY_SECONDS) -> int:
    """Re-reads the jobs of every completed run with an unfinished job. Returns how many jobs it corrected."""
    if delay:
        await asyncio.sleep(delay)
    runs = state.model.runs_with_unfinished_jobs()
    if not runs:
        return 0
    github = await installation_client(state)
    if github is None:
        return 0
    fixed = 0
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
        log.info("corrected %s jobs whose completion webhook never arrived (%s runs)", fixed, len(runs))
    return fixed


def schedule_heal(state: State, payload: dict[str, Any]) -> asyncio.Task[Any] | None:
    """Fire-and-forget from the webhook handler, for every completed run."""
    if payload.get("action") != "completed":
        return None
    return state.spawn(heal(state))
