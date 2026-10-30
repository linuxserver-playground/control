"""
Reconciliation: control's state is built from webhooks, and GitHub delivers each one at most once, never while
control is down (every deploy restarts it), and never again unless asked. heal.py repairs runs and jobs the moment a
run completes; this is the backstop, from the scheduler's loop every RECONCILE_INTERVAL and once after every start:

- **Redelivery.** GitHub keeps the App's deliveries for three days. Every delivery since the previous pass that never
  reached control (no attempt answered 2xx and no row in `deliveries`) is redelivered, at most REDELIVER_ATTEMPTS
  times, and goes through the same handlers as the original. That repairs what a restart or an outage dropped.
- **Runs.** Every run control holds as unfinished, created within RUN_WINDOW, is read again with its jobs, and one
  GitHub no longer has is dropped, so a lost completion never holds the fleet pages or a batch slot until a timeout.
  One run GitHub cannot answer for is skipped, not the pass.
- **Threads.** Every open pull request and issue control holds is read again, and the org's open ones it lacks are
  added (one search each), so a lost close or open never lingers on the fleet pages or for triage.

Each pass is one log line and `last_reconcile` on /status. Never raises. A pass that redelivered anything or hit an
error is followed by another after RECONCILE_RETRY, not RECONCILE_INTERVAL: both mean GitHub is dropping or
refusing things right now, and that is when the backstop earns its keep.
"""

from __future__ import annotations

import datetime
import json
import logging
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any

import httpx

from . import handlers, heal
from .github import GitHub
from .github_app import app_jwt
from .model import Issue, PullRequest
from .state import State

log = logging.getLogger(__name__)

LAST_RECONCILE = "last_reconcile"
"""kv key: JSON summary of the last pass (started, finished, counts, errors), shown on /status."""
RECONCILE_INTERVAL = datetime.timedelta(minutes=30)
RECONCILE_RETRY = datetime.timedelta(minutes=5)
"""The interval after a pass that found lost deliveries or could not finish a step."""
REDELIVER_LOOKBACK = datetime.timedelta(hours=24)
"""The furthest back a pass reads GitHub's delivery log, after a long stop; normally it reads since the last pass."""
REDELIVER_MARGIN = datetime.timedelta(minutes=10)
"""Overlap with the previous pass, so a delivery that failed while it ran is not missed."""
REDELIVER_ATTEMPTS = 3
"""Redeliveries of one delivery before control gives up on it (a payload its intake refuses, say)."""
REDELIVER_PAGES = 200
"""About 20,000 deliveries: a day of the playground's traffic."""
RUN_WINDOW = datetime.timedelta(days=7)
"""Older unfinished runs are left to batch.STALE_RUN; GitHub cancels a run after 35 days at most anyway."""

_started = False
"""Whether this process has reconciled yet: the first tick after a start always does, since a restart is exactly
when deliveries are dropped."""


def _iso(at: datetime.datetime) -> str:
    return at.astimezone(datetime.UTC).isoformat(timespec="seconds")


def last_reconcile(state: State) -> dict[str, Any] | None:
    raw = state.db.get(LAST_RECONCILE)
    loaded = json.loads(raw) if raw else None
    return loaded if isinstance(loaded, dict) else None


def due(state: State, now: datetime.datetime) -> bool:
    last = last_reconcile(state)
    if not _started or not last or not last.get("started"):
        return True
    interval = RECONCILE_RETRY if troubled(last) else RECONCILE_INTERVAL
    return now - datetime.datetime.fromisoformat(str(last["started"])) >= interval


def troubled(last: dict[str, Any]) -> bool:
    """Whether the pass found GitHub dropping deliveries or failing requests."""
    counts = last.get("counts")
    redelivered = counts.get("redelivered") if isinstance(counts, dict) else None
    return bool(last.get("errors")) or bool(redelivered)


async def tick(state: State, github: GitHub, now: datetime.datetime) -> dict[str, Any] | None:
    """One pass when due; the record it stored, or None. Never raises."""
    global _started
    if not due(state, now):
        return None
    previous = last_reconcile(state)
    _started = True
    since = now - REDELIVER_LOOKBACK
    if previous and previous.get("started"):
        since = max(since, datetime.datetime.fromisoformat(str(previous["started"])) - REDELIVER_MARGIN)
    record: dict[str, Any] = {"started": _iso(now), "finished": None, "counts": {}, "errors": []}
    state.db.set(LAST_RECONCILE, json.dumps(record))
    steps: tuple[tuple[str, Callable[[], Awaitable[int]]], ...] = (
        ("redelivered", partial(redeliver, state, since)),
        ("runs", partial(runs, state, github, now, record["errors"])),
        ("threads", partial(threads, state, github)),
    )
    for name, step in steps:
        try:
            record["counts"][name] = await step()
        except Exception as exc:  # one step's failure must not stop the others; the next pass tries again
            log.exception("reconcile: %s failed", name)
            record["errors"].append(f"{name}: {type(exc).__name__}: {exc}")
    record["finished"] = _iso(datetime.datetime.now(datetime.UTC))
    state.db.set(LAST_RECONCILE, json.dumps(record))
    log.info("reconcile: %s%s", record["counts"], f", errors: {record['errors']}" if record["errors"] else "")
    return record


# -- redelivery ------------------------------------------------------------------------------------------------------


async def redeliver(state: State, since: datetime.datetime) -> int:
    """Ask GitHub to send again every delivery since `since` that never reached control. Returns how many."""
    credentials = state.credentials.load()
    if credentials is None:
        return 0
    app = GitHub(state.http, app_jwt(credentials))
    attempts: dict[str, list[dict[str, Any]]] = {}
    url: str | None = "/app/hook/deliveries"
    params: dict[str, Any] | None = {"per_page": 100}
    for _ in range(REDELIVER_PAGES):
        if url is None:
            break
        response = await app.get(url, params)
        older = False
        for item in response.json():
            at = datetime.datetime.fromisoformat(str(item["delivered_at"]).replace("Z", "+00:00"))
            if at < since:
                older = True
                continue
            attempts.setdefault(str(item["guid"]), []).append(item)
        url = None if older else response.links.get("next", {}).get("url")
        params = None  # the next link carries its own query string
    sent = 0
    for guid, items in attempts.items():
        if any(200 <= int(i.get("status_code") or 0) < 300 for i in items) or state.db.has_delivery(guid):
            continue
        if items[0].get("event") not in handlers.STORED_EVENTS:
            continue  # control would answer and drop it
        if sum(1 for i in items if i.get("redelivery")) >= REDELIVER_ATTEMPTS:
            continue
        newest = max(items, key=lambda i: str(i["delivered_at"]))
        await app.post(f"/app/hook/deliveries/{newest['id']}/attempts")
        log.info("reconcile: redelivering %s %s (%s)", newest.get("event"), newest.get("action") or "", guid)
        sent += 1
    return sent


# -- runs ------------------------------------------------------------------------------------------------------------


async def runs(state: State, github: GitHub, now: datetime.datetime, errors: list[str] | None = None) -> int:
    """Read every unfinished run of the last RUN_WINDOW again, with its jobs. Returns how many runs and jobs changed.
    A run GitHub cannot answer for (a 502 under load) is logged, summarised in `errors` and left for the next pass."""
    since = _iso(now - RUN_WINDOW)
    names: dict[str, dict[Any, str]] = {}
    changed = 0
    failed: list[str] = []
    for run in state.model.active_runs():
        if run.created_at < since:
            continue
        try:
            changed += await heal.read_run(state, github, run.repo, run.id, names)
            if state.model.run(run.id) is not None:
                changed += await heal.read_jobs(state, github, run.repo, run.id)
        except Exception as exc:  # one run's failure must not stop the others
            log.exception("reconcile: reading %s run %s failed", run.repo, run.id)
            failed.append(f"{run.repo} run {run.id}: {type(exc).__name__}: {exc}")
    if failed and errors is not None:
        errors.append(f"runs: {len(failed)} could not be read; first: {failed[0]}")
    return changed


# -- threads ---------------------------------------------------------------------------------------------------------


async def threads(state: State, github: GitHub) -> int:
    """Read every open pull request and issue control holds again, and add the org's open ones it lacks. Returns how
    many changed state or were added."""
    changed = 0
    for pr in state.model.open_pull_requests():
        if (data := await _get(github, f"/repos/{pr.repo}/pulls/{pr.number}")) is not None:
            changed += _store_pull(state, PullRequest.from_github(pr.repo, data))
    for issue in state.model.open_issues():
        if (data := await _get(github, f"/repos/{issue.repo}/issues/{issue.number}")) is not None:
            changed += _store_issue(state, Issue.from_github(issue.repo, data))
    org = state.settings.github_org
    for kind in ("pr", "issue"):
        found = await github.paginate(
            "/search/issues", {"q": f"org:{org} is:{kind} is:open", "per_page": 100}, key="items", limit=1000
        )
        for item in found:
            repo = str(item["repository_url"]).split("/repos/", 1)[1]
            number = int(item["number"])
            if kind == "issue" and state.model.issue(repo, number) is None:
                changed += _store_issue(state, Issue.from_github(repo, item))
            elif (
                kind == "pr"
                and state.model.pull_request(repo, number) is None
                and (data := await _get(github, f"/repos/{repo}/pulls/{number}")) is not None
            ):
                changed += _store_pull(state, PullRequest.from_github(repo, data))
    return changed


async def _get(github: GitHub, path: str) -> dict[str, Any] | None:
    """The object, or None when GitHub no longer has it (a deleted or transferred repository)."""
    try:
        data = (await github.get(path)).json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        raise
    return data if isinstance(data, dict) else None


def _store_pull(state: State, pr: PullRequest) -> bool:
    before = state.model.pull_request(pr.repo, pr.number)
    state.model.upsert_pull_request(pr)
    after = state.model.pull_request(pr.repo, pr.number)
    if after is None or (before is not None and before.state == after.state):
        return False
    state.stream.publish("pull_requests", [pr.repo, pr.number])
    return True


def _store_issue(state: State, issue: Issue) -> bool:
    before = state.model.issue(issue.repo, issue.number)
    state.model.upsert_issue(issue)
    after = state.model.issue(issue.repo, issue.number)
    if after is None or (before is not None and before.state == after.state):
        return False
    state.stream.publish("issues", [issue.repo, issue.number])
    return True
