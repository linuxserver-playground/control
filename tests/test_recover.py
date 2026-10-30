"""Self-healing: failed Pages deploys are redeployed with backoff, infrastructure failures of live builds are re-run
once, and only what stays broken reaches Discord."""

import datetime
import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from control import recover
from control.config import Settings
from control.fleet import manifest_row
from control.github import GitHub
from control.model import Job, Repo, Run
from control.state import State

from .conftest import REPO, fixture, mock_client, post_event

T0 = datetime.datetime(2026, 10, 28, 12, 0, tzinfo=datetime.UTC)
MANIFEST: dict[str, Any] = {"name": "sonarr", "release": {"branch": "main", "tag": "latest"}}
MINUTE = datetime.timedelta(minutes=1)


class World:
    """GitHub (jobs, dispatches, re-runs) and Discord, recording every write."""

    def __init__(self) -> None:
        self.jobs: dict[int, list[dict[str, Any]]] = {}
        self.posts: list[tuple[str, Any]] = []
        self.discord: list[str] = []
        self.rerun_status = 201

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "discord.com":
            self.discord.append(json.loads(request.content).get("content", ""))
            return httpx.Response(200, json={"id": "m1"})
        path = request.url.path
        if request.method == "GET" and path.endswith("/jobs"):
            run_id = int(path.split("/")[-2])
            return httpx.Response(200, json={"total_count": 1, "jobs": self.jobs.get(run_id, [])})
        body = json.loads(request.content) if request.content else None
        self.posts.append((path, body))
        if path.endswith("/rerun-failed-jobs"):
            return httpx.Response(self.rerun_status, json={})
        return httpx.Response(204)


def iso(at: datetime.datetime) -> str:
    return at.isoformat(timespec="seconds")


def live(state: State, branch: str = "main") -> None:
    state.model.upsert_repo(Repo.from_github({**fixture("api.repos")[0], "full_name": REPO, "name": "docker-sonarr"}))
    state.model.upsert_manifest(manifest_row(REPO, branch, f"sha-{branch}", MANIFEST))


def run(
    state: State,
    run_id: int,
    at: datetime.datetime,
    conclusion: str | None = "failure",
    event: str = "push",
    branch: str = "main",
    attempt: int = 1,
) -> None:
    data = {
        "id": run_id,
        "name": "Build",
        "event": event,
        "head_branch": branch,
        "head_sha": "a" * 40,
        "status": "completed" if conclusion else "in_progress",
        "conclusion": conclusion,
        "html_url": f"https://github.com/{REPO}/actions/runs/{run_id}",
        "created_at": iso(at - datetime.timedelta(minutes=20)),
        "updated_at": iso(at),
        "run_attempt": attempt,
    }
    state.model.upsert_run(Run.from_github(REPO, data))


def step(name: str, conclusion: str = "success", number: int = 1) -> dict[str, Any]:
    return {"name": name, "status": "completed", "conclusion": conclusion, "number": number}


def job_data(
    run_id: int, job_id: int, name: str, conclusion: str, steps: list[dict[str, Any]], at: datetime.datetime = T0
) -> dict[str, Any]:
    return {
        "id": job_id,
        "run_id": run_id,
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
        "created_at": iso(at - datetime.timedelta(minutes=5)),
        "started_at": iso(at - datetime.timedelta(minutes=5)),
        "completed_at": iso(at),
        "html_url": f"https://github.com/{REPO}/actions/runs/{run_id}/job/{job_id}",
        "labels": [],
        "steps": steps,
    }


def jobs(*items: dict[str, Any]) -> list[Job]:
    return [Job.from_github(REPO, item) for item in items]


def settings_of(state: State, monkeypatch: pytest.MonkeyPatch, **update: Any) -> None:
    monkeypatch.setattr(state, "settings", state.settings.model_copy(update=update))


async def tick(state: State, world: World, monkeypatch: pytest.MonkeyPatch, at: datetime.datetime) -> int:
    async with mock_client(world) as http:
        monkeypatch.setattr(state.discord, "client", http)
        monkeypatch.setattr(state.discord, "webhook_url", "https://discord.com/api/webhooks/1/t")
        return await recover.tick(state, GitHub(http, "ghs_token"), at)


# -- judging --------------------------------------------------------------------------------------------------------

PREPARE_OK = job_data(1, 10, "build / Prepare", "success", [step("Check out repository")])
PAGES_SKIPPED = job_data(1, 14, "build / Pages", "skipped", [])


def publish_failed(job_id: int = 13) -> dict[str, Any]:
    return job_data(
        1,
        job_id,
        "build / Publish",
        "failure",
        [step("Determine result"), step("Fail if any job failed", "failure", 9)],
    )


def test_a_registry_login_failure_is_infrastructure() -> None:
    arch = job_data(1, 11, "build / Build amd64", "failure", [step("Set up job"), step("Log in to GHCR", "failure", 5)])
    other = job_data(1, 12, "build / Build arm64", "success", [step("Build")])
    verdict = recover.judge(jobs(PREPARE_OK, arch, other, publish_failed(), PAGES_SKIPPED))
    assert verdict.infra and verdict.step == "build / Build amd64: Log in to GHCR"
    assert verdict.slots == 2  # the arch leg and Publish; the skipped Pages job is not part of a build


@pytest.mark.parametrize(
    ("steps", "infra"),
    [
        ([step("Check out repository", "failure")], True),
        ([step("Set up buildx"), step("Build", "failure", 2)], False),
        ([step("Build"), step("Test", "failure", 2)], False),
        ([step("Test"), step("Fail on test failure", "failure", 3)], False),
        ([step("Shellcheck and s6 permissions", "failure")], False),
        ([step("Read manifest", "failure")], False),
        ([step("Build"), step("Post Set up buildx", "failure", 9)], True),
        ([step("Set up buildx", "cancelled")], True),
        ([step("Build", "cancelled")], False),
        ([], True),  # no step at all: the runner never started or was lost
        # The workflow's own second attempt at a checkout failed too: still infrastructure.
        ([step("Check out repository", "failure"), step("Check out repository (retry)", "failure", 2)], True),
        # The first attempt failed, its twin succeeded, the image failed: the image's problem, no re-run.
        (
            [
                step("Check out repository", "failure"),
                step("Check out repository (retry)", "success", 2),
                step("Build", "failure", 5),
            ],
            False,
        ),
    ],
)
def test_only_plain_infrastructure_steps_qualify(steps: list[dict[str, Any]], infra: bool) -> None:
    arch = job_data(1, 11, "build / Build amd64", "failure", steps)
    assert recover.judge(jobs(PREPARE_OK, arch, publish_failed())).infra is infra


def test_publish_failing_alone_on_its_last_step_is_not_infrastructure() -> None:
    assert not recover.judge(jobs(PREPARE_OK, publish_failed())).infra
    release = job_data(
        1,
        13,
        "build / Publish",
        "failure",
        [step("Release (live)", "failure", 6), step("Fail if any job failed", "failure", 9)],
    )
    assert recover.judge(jobs(PREPARE_OK, release)).infra


def test_one_real_failure_beside_a_flake_means_no_rerun() -> None:
    flake = job_data(1, 11, "build / Build amd64", "failure", [step("Log in to GHCR", "failure")])
    broken = job_data(1, 12, "build / Build arm64", "failure", [step("Build", "failure")])
    verdict = recover.judge(jobs(PREPARE_OK, flake, broken, publish_failed()))
    assert not verdict.infra and verdict.step == "build / Build arm64: Build"


# -- re-runs --------------------------------------------------------------------------------------------------------


def flaky_run(state: State, world: World, run_id: int = 1, at: datetime.datetime = T0) -> None:
    run(state, run_id, at)
    arch = job_data(run_id, run_id * 10 + 1, "build / Build amd64", "failure", [step("Log in to GHCR", "failure")], at)
    prepare = {**PREPARE_OK, "id": run_id * 10, "run_id": run_id}
    world.jobs[run_id] = [prepare, arch, {**publish_failed(run_id * 10 + 3), "run_id": run_id}]


@pytest.mark.anyio
async def test_an_infrastructure_failure_is_rerun_once_after_the_delay(
    state: State, monkeypatch: pytest.MonkeyPatch
) -> None:
    live(state)
    world = World()
    flaky_run(state, world)
    assert await tick(state, world, monkeypatch, T0 + 2 * MINUTE) == 0  # still within the delay
    assert world.posts == []
    assert await tick(state, world, monkeypatch, T0 + 6 * MINUTE) == 1
    assert world.posts == [(f"/repos/{REPO}/actions/runs/1/rerun-failed-jobs", None)]
    heal = state.model.run_heal(REPO, 1)
    assert heal is not None and heal.state == "rerun" and heal.step == "build / Build amd64: Log in to GHCR"
    action = state.model.recent_actions(1)[0]
    assert (action.actor, action.action, action.target, action.result) == ("control", "rerun", "1", "ok")
    assert "Log in to GHCR" in (action.detail or "")
    # Never twice, even while the run still shows the attempt that failed.
    assert await tick(state, world, monkeypatch, T0 + 8 * MINUTE) == 0 and len(world.posts) == 1
    # The re-run passes: nothing to report.
    run(state, 1, T0 + 20 * MINUTE, "success", attempt=2)
    await tick(state, world, monkeypatch, T0 + 21 * MINUTE)
    heal = state.model.run_heal(REPO, 1)
    assert heal is not None and heal.state == "passed" and world.discord == []


@pytest.mark.anyio
async def test_a_rerun_that_fails_again_is_reported_grouped(state: State, monkeypatch: pytest.MonkeyPatch) -> None:
    live(state)
    world = World()
    flaky_run(state, world, 1)
    flaky_run(state, world, 2, T0 + MINUTE)
    state.model.upsert_manifest(manifest_row(REPO, "develop", "sha-d", MANIFEST))
    run(state, 2, T0 + MINUTE, branch="develop")
    assert await tick(state, world, monkeypatch, T0 + 7 * MINUTE) == 2
    run(state, 1, T0 + 20 * MINUTE, "failure", attempt=2)
    run(state, 2, T0 + 20 * MINUTE, "failure", branch="develop", attempt=2)
    await tick(state, world, monkeypatch, T0 + 21 * MINUTE)
    assert len(world.discord) == 1
    text = world.discord[0]
    assert "2 live builds failed again" in text and "Log in to GHCR" in text
    assert f"(<https://github.com/{REPO}/actions/runs/1>)" in text
    # Reported once; the attempts that failed again are never re-run.
    await tick(state, world, monkeypatch, T0 + 90 * MINUTE)
    assert len(world.discord) == 1 and len(world.posts) == 2


@pytest.mark.anyio
async def test_real_failures_pull_requests_and_other_branches_are_left_alone(
    state: State, monkeypatch: pytest.MonkeyPatch
) -> None:
    live(state)
    world = World()
    run(state, 1, T0)
    world.jobs[1] = [PREPARE_OK, job_data(1, 11, "build / Build amd64", "failure", [step("Build", "failure")])]
    run(state, 2, T0, event="pull_request")
    run(state, 3, T0, branch="feature")
    run(state, 4, T0, attempt=2)
    assert await tick(state, world, monkeypatch, T0 + 10 * MINUTE) == 0
    assert world.posts == []
    heal = state.model.run_heal(REPO, 1)
    assert heal is not None and heal.state == "declined" and heal.step == "build / Build amd64: Build"
    assert [state.model.run_heal(REPO, n) for n in (2, 3, 4)] == [None, None, None]


@pytest.mark.anyio
async def test_a_rerun_waits_for_capacity_and_yields_to_a_newer_run(
    state: State, monkeypatch: pytest.MonkeyPatch
) -> None:
    live(state)
    world = World()
    flaky_run(state, world)
    # Another repo fills every slot but the reserve.
    other = "example-org/docker-radarr"
    state.model.upsert_run(
        Run.from_github(other, {"id": 50, "status": "in_progress", "created_at": iso(T0), "updated_at": iso(T0)})
    )
    for n in range(16):
        state.model.upsert_job(
            Job.from_github(other, {**job_data(50, 500 + n, f"job {n}", "success", []), "status": "in_progress"})
        )
    assert await tick(state, world, monkeypatch, T0 + 6 * MINUTE) == 0
    heal = state.model.run_heal(REPO, 1)
    assert heal is not None and heal.state == "pending" and world.posts == []
    # A push built the branch meanwhile: the old run is not re-run at all.
    run(state, 2, T0 + 10 * MINUTE, "success")
    await tick(state, world, monkeypatch, T0 + 11 * MINUTE)
    heal = state.model.run_heal(REPO, 1)
    assert heal is not None and heal.state == "superseded" and world.posts == [] and world.discord == []


@pytest.mark.anyio
async def test_a_refused_rerun_is_given_up_and_reported(state: State, monkeypatch: pytest.MonkeyPatch) -> None:
    live(state)
    world = World()
    flaky_run(state, world)
    world.rerun_status = 403
    await tick(state, world, monkeypatch, T0 + 6 * MINUTE)
    heal = state.model.run_heal(REPO, 1)
    assert heal is not None and heal.state == "gave-up" and "403" in (heal.note or "")
    assert state.model.recent_actions(1)[0].result == "error"
    assert len(world.discord) == 1 and "could not re-run 1 failed live build" in world.discord[0]


# -- Pages ----------------------------------------------------------------------------------------------------------


def pages_job(state: State, run_id: int, conclusion: str, at: datetime.datetime) -> None:
    run(state, run_id, at, conclusion, event="workflow_dispatch")
    state.model.upsert_job(Job.from_github(REPO, job_data(run_id, run_id * 10, "build / Prepare", "skipped", [], at)))
    failed = [step("Check out shared workflows"), step("Deploy Pages", conclusion, 2)]
    state.model.upsert_job(
        Job.from_github(REPO, job_data(run_id, run_id * 10 + 1, "build / Pages", conclusion, failed, at))
    )


def dispatches(world: World) -> list[Any]:
    return [body for path, body in world.posts if path.endswith("/dispatches")]


@pytest.mark.anyio
async def test_a_failed_pages_deploy_is_retried_with_backoff_until_one_succeeds(
    state: State, monkeypatch: pytest.MonkeyPatch
) -> None:
    live(state)
    world = World()
    pages_job(state, 7, "failure", T0)
    assert await tick(state, world, monkeypatch, T0 + MINUTE) == 0
    row = state.model.pages_heal(REPO)
    assert row is not None and row.state == "retrying" and row.next_attempt_at == iso(T0 + 5 * MINUTE)
    assert row.note == "Deploy Pages failed"
    assert await tick(state, world, monkeypatch, T0 + 5 * MINUTE) == 1
    assert dispatches(world) == [{"ref": "main", "inputs": {"reason": "pages"}}]
    decision = state.model.recent_decisions(1)[0]
    assert decision.kind == "heal" and decision.outcome == "dispatch" and decision.info["dispatch_reason"] == "pages"
    # A Pages retry never starts the branch's build cooldown.
    assert state.model.last_decision("sonarr", "main", outcome="dispatch") is None
    action = state.model.recent_actions(1)[0]
    assert (action.actor, action.action, action.target) == ("control", "dispatch", f"{REPO}@main (pages)")
    row = state.model.pages_heal(REPO)
    assert row is not None and row.attempts == 1 and row.next_attempt_at == iso(T0 + 35 * MINUTE)
    # Its run fails too, and the next retry waits its 30 minutes.
    pages_job(state, 8, "failure", T0 + 9 * MINUTE)
    assert await tick(state, world, monkeypatch, T0 + 20 * MINUTE) == 0
    assert await tick(state, world, monkeypatch, T0 + 35 * MINUTE) == 1
    # A deploy that works clears it, whoever started it.
    pages_job(state, 9, "success", T0 + 40 * MINUTE)
    await tick(state, world, monkeypatch, T0 + 41 * MINUTE)
    row = state.model.pages_heal(REPO)
    assert row is not None and row.state == "ok" and row.attempts == 0
    assert await tick(state, world, monkeypatch, T0 + 10 * 60 * MINUTE) == 0
    assert len(dispatches(world)) == 2 and world.discord == []


@pytest.mark.anyio
async def test_pages_retries_wait_for_a_free_slot_and_a_quiet_repo(
    state: State, monkeypatch: pytest.MonkeyPatch
) -> None:
    live(state)
    world = World()
    pages_job(state, 7, "failure", T0)
    run(state, 8, T0 + 3 * MINUTE, None)  # a live build going: its Publish deploys Pages anyway
    assert await tick(state, world, monkeypatch, T0 + 6 * MINUTE) == 0
    run(state, 8, T0 + 7 * MINUTE, "success")
    assert await tick(state, world, monkeypatch, T0 + 8 * MINUTE) == 1


@pytest.mark.anyio
async def test_pages_give_up_after_the_last_retry_and_report_once(
    state: State, monkeypatch: pytest.MonkeyPatch
) -> None:
    live(state)
    settings_of(state, monkeypatch, heal_pages_attempts=2, heal_pages_backoff_minutes=(5, 30))
    world = World()
    pages_job(state, 7, "failure", T0)
    assert await tick(state, world, monkeypatch, T0 + 5 * MINUTE) == 1
    pages_job(state, 8, "failure", T0 + 10 * MINUTE)
    assert await tick(state, world, monkeypatch, T0 + 35 * MINUTE) == 1
    pages_job(state, 9, "failure", T0 + 40 * MINUTE)
    await tick(state, world, monkeypatch, T0 + 41 * MINUTE)
    row = state.model.pages_heal(REPO)
    assert row is not None and row.state == "gave-up"
    assert len(world.discord) == 1
    assert (
        "gave up redeploying the Pages site of 1 repo" in world.discord[0] and "Deploy Pages failed" in world.discord[0]
    )
    # Still broken, still quiet: no more dispatches, no second post.
    pages_job(state, 10, "failure", T0 + 3 * 60 * MINUTE)
    await tick(state, world, monkeypatch, T0 + 5 * 60 * MINUTE)
    assert len(dispatches(world)) == 2 and len(world.discord) == 1


@pytest.mark.anyio
async def test_a_live_publish_pages_failure_starts_retries(state: State, monkeypatch: pytest.MonkeyPatch) -> None:
    live(state)
    world = World()
    run(state, 20, T0, "success")
    steps = [step("Release (live)"), step("Notify control plane", number=2), step("Pages (live)", "failure", 3)]
    state.model.upsert_job(Job.from_github(REPO, job_data(20, 200, "build / Publish", "success", steps)))
    await tick(state, world, monkeypatch, T0 + MINUTE)
    row = state.model.pages_heal(REPO)
    assert row is not None and row.state == "retrying" and "Pages (live)" in (row.note or "")


def test_the_build_summary_field_starts_and_clears_retries(state: State) -> None:
    live(state)
    summary = {**fixture("control.build.summary"), recover.PAGES_FIELD: recover.PAGES_FAILED}
    recover.observe_summary(state, summary, now=lambda: T0)
    row = state.model.pages_heal(REPO)
    assert row is not None and row.state == "retrying" and row.run_id == 5001
    recover.observe_summary(state, {**summary, recover.PAGES_FIELD: recover.PAGES_DEPLOYED}, now=lambda: T0 + MINUTE)
    row = state.model.pages_heal(REPO)
    assert row is not None and row.state == "ok"
    # Without the field, or for a build that is not live, nothing changes.
    recover.observe_summary(state, fixture("control.build.summary"), now=lambda: T0 + 2 * MINUTE)
    recover.observe_summary(state, {**summary, "mode": "dev"}, now=lambda: T0 + 3 * MINUTE)
    row = state.model.pages_heal(REPO)
    assert row is not None and row.state == "ok"


@pytest.mark.anyio
async def test_a_repo_without_a_pages_site_is_not_retried(state: State, monkeypatch: pytest.MonkeyPatch) -> None:
    live(state)
    state.model.report_pages_missing(REPO, "no site")
    world = World()
    pages_job(state, 7, "failure", T0)
    assert await tick(state, world, monkeypatch, T0 + 6 * MINUTE) == 0
    row = state.model.pages_heal(REPO)
    assert row is not None and row.state == "gave-up" and world.posts == [] and world.discord == []


@pytest.mark.anyio
async def test_switches_off(state: State, monkeypatch: pytest.MonkeyPatch) -> None:
    live(state)
    settings_of(state, monkeypatch, heal_pages=False, heal_reruns=False)
    world = World()
    flaky_run(state, world)
    pages_job(state, 7, "failure", T0)
    assert await tick(state, world, monkeypatch, T0 + 10 * MINUTE) == 0
    assert state.model.pages_heal(REPO) is None and state.model.run_heal(REPO, 1) is None


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONTROL_HEAL_PAGES", "false")
    monkeypatch.setenv("CONTROL_HEAL_PAGES_BACKOFF_MINUTES", "1, 10")
    monkeypatch.setenv("CONTROL_HEAL_PAGES_ATTEMPTS", "6")
    monkeypatch.setenv("CONTROL_HEAL_RERUNS", "off")
    monkeypatch.setenv("CONTROL_HEAL_RERUN_DELAY_MINUTES", "2")
    monkeypatch.setenv("CONTROL_HEAL_RERUN_WINDOW_HOURS", "3")
    monkeypatch.setenv("CONTROL_HEAL_PER_TICK", "1")
    settings = Settings.from_env()
    assert (settings.heal_pages, settings.heal_pages_backoff_minutes, settings.heal_pages_attempts) == (
        False,
        (1, 10),
        6,
    )
    assert (settings.heal_reruns, settings.heal_rerun_delay_minutes, settings.heal_rerun_window_hours) == (False, 2, 3)
    assert settings.heal_per_tick == 1
    defaults = Settings()
    assert defaults.heal_pages_backoff_minutes == (5, 30, 120, 1440) and defaults.heal_pages_attempts == 4
    with pytest.raises(ValueError):
        Settings(heal_pages_backoff_minutes=())


def test_events_feed_the_pages_outcome(client: TestClient, state: State) -> None:
    summary = {**fixture("control.build.summary"), recover.PAGES_FIELD: recover.PAGES_FAILED}
    assert post_event(client, summary).status_code == 202
    row = state.model.pages_heal(REPO)
    assert row is not None and row.state == "retrying" and row.run_id == 5001
