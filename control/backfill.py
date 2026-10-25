"""
Cold start from the GitHub API: the webhooks only tell us what happens from now on, so a fresh database
(or one that missed deliveries while control was down) is filled by listing the org's repos and, per
repo, the recent workflow runs with their jobs, open pull requests, open issues and recent releases.
The REST objects are the same JSON the webhooks carry, so the model's converters are shared. Every write
is an idempotent upsert, so running this at any time is safe; `X-RateLimit-Remaining` is watched and we
sleep until the reset when it gets low.
"""

import asyncio
import datetime
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .github_app import API
from .model import Issue, Job, Model, PullRequest, Release, Repo, Run, is_build_run
from .state import LAST_BACKFILL, State
from .stream import Broadcaster

log = logging.getLogger(__name__)

RUNS_PER_REPO = 50
RELEASES_PER_REPO = 20
RATE_LIMIT_FLOOR = 50
"""Below this many remaining requests we wait for the window to reset rather than run into 403s."""
MAX_SLEEP = 3600.0


class Backfill:
    def __init__(
        self,
        http: httpx.AsyncClient,
        token: str,
        org: str,
        model: Model,
        stream: Broadcaster,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.http = http
        self.org = org
        self.model = model
        self.stream = stream
        self.sleep = sleep
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self.requests = 0
        self.counts: dict[str, int] = {"repos": 0, "runs": 0, "jobs": 0, "pulls": 0, "issues": 0, "releases": 0}

    async def get(self, url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        """One GET with the rate limit respected: wait before the window is exhausted, and once more on a 403/429."""
        for attempt in (1, 2):
            response = await self.http.get(url, params=params, headers=self.headers)
            self.requests += 1
            remaining = response.headers.get("X-RateLimit-Remaining")
            reset = response.headers.get("X-RateLimit-Reset")
            if (
                response.status_code in (403, 429)
                and attempt == 1
                and (remaining == "0" or "Retry-After" in response.headers)
            ):
                await self.sleep(_wait_seconds(reset, response.headers.get("Retry-After")))
                continue
            response.raise_for_status()
            if remaining is not None and remaining.isdigit() and int(remaining) < RATE_LIMIT_FLOOR:
                wait = _wait_seconds(reset, None)
                log.info("rate limit low (%s left), sleeping %.0fs", remaining, wait)
                await self.sleep(wait)
            return response
        raise AssertionError("unreachable")

    async def paginate(
        self, url: str, params: dict[str, Any], key: str | None = None, limit: int | None = None
    ) -> list[Any]:
        """Follow `Link: rel="next"` until `limit` items (or the end). `key` picks the list out of an envelope."""
        items: list[Any] = []
        next_url: str | None = url
        next_params: dict[str, Any] | None = params
        while next_url and (limit is None or len(items) < limit):
            response = await self.get(next_url, next_params)
            body = response.json()
            page = body.get(key, []) if key else body
            if not isinstance(page, list):
                break
            items.extend(page)
            next_url = response.links.get("next", {}).get("url")
            next_params = None  # the next link carries its own query string
        return items[:limit] if limit is not None else items

    async def run(self) -> dict[str, int]:
        repos = await self.paginate(
            f"{API}/orgs/{self.org}/repos", {"per_page": 100, "type": "all", "sort": "full_name"}
        )
        for data in repos:
            if not isinstance(data, dict) or not data.get("full_name"):
                continue
            repo = Repo.from_github(data)
            self.model.upsert_repo(repo)
            self.counts["repos"] += 1
            self.stream.publish("repos", repo.full_name)
            await self.repo_runs(repo.full_name)
            await self.repo_pulls(repo.full_name)
            await self.repo_issues(repo.full_name)
            await self.repo_releases(repo.full_name)
        log.info("backfill done in %s requests: %s", self.requests, self.counts)
        return dict(self.counts)

    async def repo_runs(self, repo: str) -> None:
        runs = await self.paginate(
            f"{API}/repos/{repo}/actions/runs", {"per_page": RUNS_PER_REPO}, key="workflow_runs", limit=RUNS_PER_REPO
        )
        for data in runs:
            if not is_build_run(data):
                continue
            run = Run.from_github(repo, data)
            existing = self.model.run(run.id)
            self.model.upsert_run(run)
            self.counts["runs"] += 1
            # A run we already saw finish, jobs and all, cannot change; skip its jobs call.
            if existing and not existing.active and run.status == "completed" and self.model.jobs_for_run(run.id):
                continue
            jobs = await self.paginate(
                f"{API}/repos/{repo}/actions/runs/{run.id}/jobs", {"per_page": 100}, key="jobs", limit=200
            )
            for job_data in jobs:
                self.model.upsert_job(Job.from_github(repo, job_data))
                self.counts["jobs"] += 1
            self.stream.publish("runs", run.id)

    async def repo_pulls(self, repo: str) -> None:
        for data in await self.paginate(f"{API}/repos/{repo}/pulls", {"state": "open", "per_page": 100}):
            pr = PullRequest.from_github(repo, data)
            self.model.upsert_pull_request(pr)
            self.counts["pulls"] += 1
            self.stream.publish("pull_requests", [repo, pr.number])

    async def repo_issues(self, repo: str) -> None:
        for data in await self.paginate(f"{API}/repos/{repo}/issues", {"state": "open", "per_page": 100}):
            if "pull_request" in data:
                continue
            issue = Issue.from_github(repo, data)
            self.model.upsert_issue(issue)
            self.counts["issues"] += 1
            self.stream.publish("issues", [repo, issue.number])

    async def repo_releases(self, repo: str) -> None:
        releases = await self.paginate(
            f"{API}/repos/{repo}/releases", {"per_page": RELEASES_PER_REPO}, limit=RELEASES_PER_REPO
        )
        for data in releases:
            if data.get("draft"):
                continue
            release = Release.from_github(repo, data)
            self.model.upsert_release(release)
            self.counts["releases"] += 1
            self.stream.publish("releases", [repo, release.tag])


def _wait_seconds(reset: str | None, retry_after: str | None) -> float:
    if retry_after and retry_after.isdigit():
        return min(MAX_SLEEP, float(retry_after) + 1)
    if reset and reset.isdigit():
        now = datetime.datetime.now(datetime.UTC).timestamp()
        return min(MAX_SLEEP, max(1.0, float(reset) - now + 1))
    return 60.0


async def backfill_once(state: State) -> dict[str, Any]:
    """Run one backfill with the installation token and record the outcome under `last_backfill` in kv."""
    started = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    record: dict[str, Any] = {"started": started, "finished": None, "counts": None, "error": None}
    credentials = state.credentials.load()
    installation_id = state.installation_id
    if credentials is None or installation_id is None:
        record["error"] = "GitHub App not configured or not installed"
        state.db.set(LAST_BACKFILL, json.dumps(record))
        log.warning("backfill skipped: %s", record["error"])
        return record
    state.db.set(LAST_BACKFILL, json.dumps(record))
    try:
        token = await state.tokens.get(credentials, installation_id)
        backfill = Backfill(state.http, token, state.settings.github_org, state.model, state.stream)
        record["counts"] = await backfill.run()
    except Exception as exc:
        log.exception("backfill failed")
        record["error"] = f"{type(exc).__name__}: {exc}"
    record["finished"] = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    state.db.set(LAST_BACKFILL, json.dumps(record))
    return record


def last_backfill(state: State) -> dict[str, Any] | None:
    raw = state.db.get(LAST_BACKFILL)
    if not raw:
        return None
    loaded = json.loads(raw)
    return loaded if isinstance(loaded, dict) else None
