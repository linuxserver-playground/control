from fastapi.testclient import TestClient

from control import handlers
from control.github_app import AppCredentials
from control.model import Model
from control.state import State
from control.stream import Broadcaster

from .conftest import FINISHED_SEED, REPO, deliver, fixture, seed


def test_workflow_run_progresses_and_never_regresses(state: State) -> None:
    seed(state, "workflow_run.requested")
    run = state.model.run(5001)
    assert run and (run.status, run.conclusion, run.workflow_name, run.branch) == (
        "queued",
        None,
        "Build image",
        "main",
    )
    assert run.repo == REPO and run.actor == "octocat" and run.run_number == 42 and run.attempt == 1
    assert run.created_at == "2026-10-24T12:00:00+00:00"  # GitHub's Z timestamps are normalised

    seed(state, "workflow_run.in_progress", "workflow_run.completed")
    run = state.model.run(5001)
    assert run and (run.status, run.conclusion) == ("completed", "success")
    assert run.started_at == "2026-10-24T12:00:05+00:00"

    # A late in_progress delivery (older updated_at) must not move the run backwards.
    seed(state, "workflow_run.in_progress")
    run = state.model.run(5001)
    assert run and run.status == "completed"
    assert state.model.counts()["runs"] == 1


def test_a_skipped_run_stays_completed_when_in_progress_arrives_last(state: State) -> None:
    """A run whose jobs are all skipped is requested, in progress and completed in one second."""
    at = "2026-10-29T11:01:22Z"
    payloads = {name: fixture(f"workflow_run.{name}") for name in ("in_progress", "completed")}
    for payload in payloads.values():
        payload["workflow_run"]["updated_at"] = at
    payloads["completed"]["workflow_run"]["conclusion"] = "skipped"
    handlers.process(state.model, state.stream, "workflow_run", payloads["completed"])
    handlers.process(state.model, state.stream, "workflow_run", payloads["in_progress"])
    run = state.model.run(5001)
    assert run and (run.status, run.conclusion) == ("completed", "skipped")

    # A re-run is a new attempt: it moves the run back to in progress.
    rerun = fixture("workflow_run.in_progress")
    rerun["workflow_run"].update(run_attempt=2, updated_at="2026-10-29T12:00:00Z")
    handlers.process(state.model, state.stream, "workflow_run", rerun)
    run = state.model.run(5001)
    assert run and (run.status, run.attempt) == ("in_progress", 2)
    # And a late delivery of the first attempt cannot undo it.
    handlers.process(state.model, state.stream, "workflow_run", payloads["completed"])
    run = state.model.run(5001)
    assert run and (run.status, run.attempt) == ("in_progress", 2)


def test_workflow_job_keeps_steps_runner_and_labels(state: State) -> None:
    seed(state, "workflow_job.queued")
    job = state.model.job(9001)
    assert job and job.status == "queued" and job.runner_name is None and job.labels == ["ubuntu-latest"]

    seed(state, "workflow_job.in_progress")
    job = state.model.job(9001)
    assert job and job.status == "in_progress" and job.runner_name == "GitHub Actions 11"
    assert [s.status for s in job.steps] == ["completed", "in_progress"]

    seed(state, "workflow_job.completed")
    job = state.model.job(9001)
    assert job and (job.status, job.conclusion, job.runner_group) == ("completed", "success", "GitHub Actions")
    assert [(s.name, s.conclusion) for s in job.steps] == [("Set up job", "success"), ("Build", "success")]
    assert job.duration_seconds() == 535.0
    assert state.model.jobs_for_run(5001) == [job]

    seed(state, "workflow_job.in_progress")  # stale: ignored by the status rank guard
    job = state.model.job(9001)
    assert job and job.status == "completed"


def test_pull_request_lifecycle_and_review(state: State) -> None:
    seed(state, "pull_request.opened")
    pr = state.model.pull_request(REPO, 7)
    assert pr and (pr.state, pr.draft, pr.head_fork, pr.author, pr.review_state) == (
        "open",
        False,
        True,
        "contributor",
        None,
    )
    assert pr.head_sha == "b" * 40

    seed(state, "pull_request.synchronize", "pull_request_review.submitted")
    pr = state.model.pull_request(REPO, 7)
    assert pr and pr.head_sha == "c" * 40 and pr.review_state == "approved"

    seed(state, "pull_request.synchronize")  # the review verdict survives payloads that do not carry one
    pr = state.model.pull_request(REPO, 7)
    assert pr and pr.review_state == "approved"

    seed(state, "pull_request.closed")
    pr = state.model.pull_request(REPO, 7)
    assert pr and pr.state == "closed" and pr.merged_at == "2026-10-23T11:00:00+00:00"
    assert state.model.open_pull_requests() == []


def test_issues_track_labels_and_state(state: State) -> None:
    seed(state, "issues.opened")
    issue = state.model.issue(REPO, 12)
    assert issue and issue.labels == ["bug", "needs-triage"] and issue.author == "reporter"
    seed(state, "issues.labeled")
    issue = state.model.issue(REPO, 12)
    assert issue and issue.labels == ["bug", "needs-triage", "confirmed"]
    seed(state, "issues.closed")
    assert state.model.open_issues() == []

    # GitHub raises `issues` for pull requests too; those belong to the PR handler.
    payload = fixture("issues.opened")
    payload["issue"]["number"] = 99
    payload["issue"]["pull_request"] = {"url": "x"}
    handlers.process(state.model, state.stream, "issues", payload)
    assert state.model.issue(REPO, 99) is None


def test_comments_move_the_thread_forward(state: State) -> None:
    seed(state, "issues.opened", "issue_comment.created")
    issue = state.model.issue(REPO, 12)
    assert issue and issue.updated_at == "2026-10-22T09:00:00+00:00"

    seed(state, "pull_request.opened")
    before = state.model.pull_request(REPO, 7)
    assert before
    payload = fixture("issue_comment.created")
    payload["issue"].update(number=7, pull_request={"url": "x"}, updated_at="2030-01-01T00:00:00Z")
    handlers.process(state.model, state.stream, "issue_comment", payload)
    pr = state.model.pull_request(REPO, 7)
    assert pr and pr.updated_at == "2030-01-01T00:00:00+00:00" and pr.head_sha == before.head_sha

    # An older comment delivery never moves it back, and a comment on an unknown PR inserts nothing.
    payload["issue"].update(updated_at="2029-01-01T00:00:00Z")
    handlers.process(state.model, state.stream, "issue_comment", payload)
    pr = state.model.pull_request(REPO, 7)
    assert pr and pr.updated_at == "2030-01-01T00:00:00+00:00"
    payload["issue"].update(number=8)
    handlers.process(state.model, state.stream, "issue_comment", payload)
    assert state.model.pull_request(REPO, 8) is None


def test_release_published_then_deleted(state: State) -> None:
    seed(state, "release.published")
    release = state.model.release(REPO, "4.0.20.3014-ls1")
    assert release and release.published_at == "2026-10-24T12:10:30+00:00" and not release.prerelease
    assert release.body and "Upstream changes" in release.body
    seed(state, "release.deleted")
    assert state.model.release(REPO, "4.0.20.3014-ls1") is None


def test_repository_events_and_push_keep_repos_current(state: State) -> None:
    seed(state, "repository.created")
    radarr = state.model.repo("example-org/docker-radarr")
    assert radarr and radarr.is_image and radarr.image_name == "radarr" and not radarr.archived

    seed(state, "push")  # the push fixture carries a changed default branch
    sonarr = state.model.repo(REPO)
    assert sonarr and sonarr.default_branch == "master" and sonarr.image_name == "sonarr"

    seed(state, "repository.archived")
    sonarr = state.model.repo(REPO)
    assert sonarr and sonarr.archived

    payload = fixture("repository.created")
    payload["action"] = "deleted"
    handlers.process(state.model, state.stream, "repository", payload)
    assert state.model.repo("example-org/docker-radarr") is None
    assert [r.name for r in state.model.repos(images_only=True)] == ["docker-sonarr"]


def test_non_image_repos_are_marked(state: State) -> None:
    payload = fixture("push")
    payload["repository"].update(name="workflows", full_name="example-org/workflows")
    handlers.process(state.model, state.stream, "push", payload)
    repo = state.model.repo("example-org/workflows")
    assert repo and not repo.is_image and repo.image_name is None
    assert state.model.repos(images_only=True) == []


def test_control_events_become_builds(state: State) -> None:
    seed(state, "control.build.started")
    build = state.model.build(REPO, 5001)
    assert build and build.running and build.mode == "live" and build.image == "sonarr" and build.version_tag is None

    seed(state, "control.build.summary")
    build = state.model.build(REPO, 5001)
    assert build and not build.running and build.result == "success"
    assert (build.version_tag, build.release_tag, build.upstream, build.semver, build.ls_build) == (
        "4.0.20.3014-ls1",
        "latest",
        "4.0.20.3014",
        "4.0.20",
        1,
    )
    assert build.archs == ["amd64", "arm64"] and build.test_success is True
    assert build.bundle and build.bundle["schema"] == 1

    seed(state, "control.build.started")  # a late start never erases a summary
    build = state.model.build(REPO, 5001)
    assert build and build.result == "success"


def test_failed_summary_without_bundle(state: State) -> None:
    seed(state, "control.build.summary.failed")
    build = state.model.build(REPO, 5001)
    assert build and build.result == "failure" and build.failed_job == "build (arm64)"
    assert build.bundle is None and build.version_tag is None and build.test_success is None


def test_unknown_events_and_bad_payloads_never_raise(state: State) -> None:
    assert handlers.process(state.model, state.stream, "check_run", {"action": "created"}) is False
    assert handlers.process(state.model, state.stream, "workflow_run", {"action": "completed"}) is False
    assert handlers.process(state.model, state.stream, "workflow_job", {"workflow_job": {"id": "x"}}) is False
    assert state.model.counts()["runs"] == 0


def test_replay_is_idempotent(client: TestClient, configured: AppCredentials, state: State) -> None:
    for number, name in enumerate(n for n in FINISHED_SEED if not n.startswith("control.")):
        payload = fixture(name)
        assert deliver(client, configured.webhook_secret, name.split(".")[0], payload, f"d-{number}").status_code == 202
    seed(state, "control.build.started", "control.build.summary")
    before = {
        "counts": state.model.counts(),
        "run": state.model.run(5001),
        "job": state.model.job(9001),
        "pr": state.model.pull_request(REPO, 7),
        "build": state.model.build(REPO, 5001),
    }
    processed, applied = handlers.replay(Model(state.db), Broadcaster())
    assert processed == applied == state.db.count_deliveries()
    assert {
        "counts": state.model.counts(),
        "run": state.model.run(5001),
        "job": state.model.job(9001),
        "pr": state.model.pull_request(REPO, 7),
        "build": state.model.build(REPO, 5001),
    } == before


def test_webhook_intake_applies_handlers(client: TestClient, configured: AppCredentials, state: State) -> None:
    response = deliver(client, configured.webhook_secret, "workflow_run", fixture("workflow_run.requested"), "wr-1")
    assert response.status_code == 202
    run = state.model.run(5001)
    assert run and run.status == "queued"
    assert state.model.repo(REPO) is not None


def test_pr_report_runs_and_jobs_are_not_builds(state: State) -> None:
    """The trusted PR report runs on the default branch; storing it would make it that branch's latest build."""
    run = fixture("workflow_run.completed")
    run["workflow_run"] = {
        **run["workflow_run"],
        "id": 5999,
        "name": "PR report",
        "path": ".github/workflows/pr-report.yml",
    }
    handlers.process(state.model, state.stream, "workflow_run", run)
    assert state.model.run(5999) is None
    job = fixture("workflow_job.queued")
    job["workflow_job"] = {**job["workflow_job"], "id": 9999, "workflow_name": "PR report"}
    handlers.process(state.model, state.stream, "workflow_job", job)
    assert state.model.job(9999) is None
    seed(state, "workflow_run.completed")
    assert state.model.run(5001) is not None
