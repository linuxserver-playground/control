"""
Turn stored deliveries into state. `process` is called by webhook intake right after a delivery is stored
and by `replay`, which walks the whole `deliveries` table in order (after a migration, or to rebuild the
model from scratch). Handlers are pure upserts keyed by GitHub ids, so processing the same delivery twice
is harmless, and nothing here may raise into a webhook response: failures are logged and skipped.
"""

import logging
from collections.abc import Callable
from typing import Any

from .model import (
    Build,
    Issue,
    Job,
    Model,
    PullRequest,
    Release,
    Repo,
    Run,
    is_build_job,
    is_build_run,
    is_pages_job,
)
from .stream import Broadcaster

log = logging.getLogger(__name__)

CONTROL_EVENT_PREFIX = "control:"
"""Events from /events are stored in `deliveries` as `control:<type>` so they replay like webhooks."""

Handler = Callable[[Model, Broadcaster, dict[str, Any]], None]


def process(model: Model, stream: Broadcaster, event: str, payload: dict[str, Any]) -> bool:
    """Apply one delivery. Returns False when the event is unknown or the handler failed (both are logged)."""
    try:
        if event not in HANDLERS:
            log.debug("no handler for %s, stored only", event)
            return False
        if (repo := payload.get("repository")) and isinstance(repo, dict) and (touched := model.touch_repo(repo)):
            stream.publish("repos", touched.full_name)
        HANDLERS[event](model, stream, payload)
        return True
    except Exception:
        log.exception("handler for %s %s failed", event, payload.get("action", ""))
        return False


def replay(model: Model, stream: Broadcaster) -> tuple[int, int]:
    """Reprocess every stored delivery in arrival order. Returns (processed, applied)."""
    processed = applied = 0
    for _, event, payload in model.db.iter_deliveries():
        processed += 1
        if process(model, stream, event, payload):
            applied += 1
    log.info("replayed %s deliveries, %s applied", processed, applied)
    return processed, applied


def _repo_name(payload: dict[str, Any]) -> str:
    repository = payload.get("repository")
    if isinstance(repository, dict) and repository.get("full_name"):
        return str(repository["full_name"])
    if isinstance(repository, str):
        return repository
    raise ValueError("payload has no repository")


def workflow_run(model: Model, stream: Broadcaster, payload: dict[str, Any]) -> None:
    data = payload["workflow_run"]
    workflow = payload.get("workflow")
    name = str(workflow["name"]) if isinstance(workflow, dict) and workflow.get("name") else None
    if not is_build_run({**data, "name": name or data.get("name")}):
        return
    run = Run.from_github(_repo_name(payload), data, name)
    if model.upsert_run(run):
        stream.publish("runs", run.id)


def workflow_job(model: Model, stream: Broadcaster, payload: dict[str, Any]) -> None:
    if not is_build_job(payload["workflow_job"]):
        return
    job = Job.from_github(_repo_name(payload), payload["workflow_job"])
    if model.upsert_job(job):
        stream.publish("jobs", job.id)
        stream.publish("runs", job.run_id)
        if payload.get("action") == "completed" and is_pages_job(job.name) and job.conclusion == "failure":
            # A Pages deploy is not a build: no event, no Discord, no red branch. Its failure is this line and the
            # run on the runs page; recover.py redeploys the site with backoff and reports it if that never works.
            log.warning("pages deploy failed on %s run %s: %s", job.repo, job.run_id, job.html_url)


def pull_request(model: Model, stream: Broadcaster, payload: dict[str, Any]) -> None:
    pr = PullRequest.from_github(_repo_name(payload), payload["pull_request"])
    if model.upsert_pull_request(pr):
        stream.publish("pull_requests", [pr.repo, pr.number])


def pull_request_review(model: Model, stream: Broadcaster, payload: dict[str, Any]) -> None:
    repo = _repo_name(payload)
    pr = PullRequest.from_github(repo, payload["pull_request"])
    model.upsert_pull_request(pr)
    review = payload.get("review") or {}
    state = str(review.get("state") or "").lower()
    # Only verdicts change the column; comments and dismissals leave the last verdict in place.
    if state in ("approved", "changes_requested"):
        model.set_review_state(repo, pr.number, state, review.get("submitted_at"))
    stream.publish("pull_requests", [repo, pr.number])


def issues(model: Model, stream: Broadcaster, payload: dict[str, Any]) -> None:
    data = payload["issue"]
    if "pull_request" in data:
        return  # GitHub also raises `issues` for pull requests; the PR handler owns those
    issue = Issue.from_github(_repo_name(payload), data)
    if model.upsert_issue(issue):
        stream.publish("issues", [issue.repo, issue.number])


def issue_comment(model: Model, stream: Broadcaster, payload: dict[str, Any]) -> None:
    """A comment moves its thread's `updated_at`, so a reader polling the open lists sees the reply (the triage bot
    answers an author's follow-up this way). The comment itself is not stored."""
    repo = _repo_name(payload)
    data = payload["issue"]
    if "pull_request" in data:
        # The payload's issue object lacks the head and fork fields, so only the timestamp moves.
        if model.touch_pull_request(repo, int(data["number"]), data.get("updated_at")):
            stream.publish("pull_requests", [repo, data["number"]])
        return
    issue = Issue.from_github(repo, data)
    if model.upsert_issue(issue):
        stream.publish("issues", [issue.repo, issue.number])


def release(model: Model, stream: Broadcaster, payload: dict[str, Any]) -> None:
    repo = _repo_name(payload)
    data = payload["release"]
    if payload.get("action") == "deleted":
        model.delete_release(repo, str(data["tag_name"]))
        stream.publish("releases", [repo, data["tag_name"]])
        return
    if data.get("draft"):
        return
    rel = Release.from_github(repo, data)
    if model.upsert_release(rel):
        stream.publish("releases", [repo, rel.tag])


def repository(model: Model, stream: Broadcaster, payload: dict[str, Any]) -> None:
    data = payload["repository"]
    if payload.get("action") == "deleted":
        model.delete_repo(str(data["full_name"]))
    else:
        model.upsert_repo(Repo.from_github(data))
    stream.publish("repos", data["full_name"])


def push(model: Model, stream: Broadcaster, payload: dict[str, Any]) -> None:
    pass  # `process` already refreshed repos.last_seen and default_branch from `repository`


def control_build(model: Model, stream: Broadcaster, payload: dict[str, Any]) -> None:
    build = Build.from_event(payload)
    if model.upsert_build(build):
        stream.publish("builds", [build.repo, build.run_id])


HANDLERS: dict[str, Handler] = {
    "workflow_run": workflow_run,
    "workflow_job": workflow_job,
    "pull_request": pull_request,
    "pull_request_review": pull_request_review,
    "issues": issues,
    "issue_comment": issue_comment,
    "release": release,
    "repository": repository,
    "push": push,
    f"{CONTROL_EVENT_PREFIX}build.started": control_build,
    f"{CONTROL_EVENT_PREFIX}build.summary": control_build,
}
