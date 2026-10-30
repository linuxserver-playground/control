import datetime
import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from control import batch, events, scheduler
from control.github import GitHub
from control.model import Job, Run
from control.state import State

from .conftest import mock_client

ORG = "example-org"
SLOT = datetime.datetime(2026, 10, 24, 4, 30, tzinfo=datetime.UTC)
"""selkies-layers' daily slot on the test day."""


def project(name: str) -> batch.Project:
    found = batch.project_named(name)
    assert found is not None
    return found


SELKIES = project("selkies-layers")
PROOT = project("proot-apps")
WHEELIE = project("wheelie")

DISTROS = {
    "distros": {
        "alpine-3.24": {"image": "x", "index": "alpine-3.24", "python": "3.14", "libc": "musl", "libc_version": "1.2"},
    },
    "arches": {
        "amd64": {"platform": "x86_64", "runner": "ubuntu-24.04"},
        "arm64": {"platform": "aarch64", "runner": "ubuntu-24.04-arm"},
    },
}


def pypi(files: list[str], version: str = "1.0") -> bytes:
    return json.dumps(
        {"info": {"version": version, "requires_python": ">=3.9"}, "urls": [{"filename": f} for f in files]}
    ).encode()


class FakeGitHub:
    """The calls the batch scheduler makes: raw file reads from wheelie, workflow dispatches, Discord posts."""

    def __init__(self, status: int = 204, packages: str = "cffi\nbluepy  # sdist only\n", index: str = "") -> None:
        self.status = status
        self.files = {
            "distros.json": json.dumps(DISTROS),
            "packages.txt": packages,
            "docs/alpine-3.24/index.html": index,
        }
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.gets: list[str] = []
        self.discord: list[list[dict[str, Any]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "discord.com":
            self.discord.append(json.loads(request.content)["embeds"])
            return httpx.Response(200, json={"id": "1"})
        assert request.headers["Authorization"] == "Bearer ghs_token"
        if request.method == "POST":
            self.posts.append((request.url.path, json.loads(request.content)))
            if self.status != 204:
                return httpx.Response(self.status, json={"message": "Resource not accessible by integration"})
            return httpx.Response(204)
        path = request.url.path
        self.gets.append(path)
        prefix = f"/repos/{ORG}/wheelie/contents/"
        if path.startswith(prefix) and path[len(prefix) :] in self.files:
            assert request.headers["Accept"] == "application/vnd.github.raw+json"
            assert request.url.params["ref"] == "main"
            return httpx.Response(200, text=self.files[path[len(prefix) :]])
        return httpx.Response(404, json={"message": "Not Found"})


class FakePyPI:
    def __init__(self, releases: dict[str, bytes]) -> None:
        self.releases = releases

    def __call__(self, url: str) -> bytes | None:
        return self.releases.get(url.rsplit("/", 2)[-2])


WHEELS = FakePyPI(
    {
        "cffi": pypi(
            [
                "cffi-1.0-cp314-cp314-musllinux_1_2_x86_64.whl",
                "cffi-1.0-cp314-cp314-musllinux_1_2_aarch64.whl",
            ]
        ),
        "bluepy": pypi(["bluepy-1.3.0.tar.gz"], "1.3.0"),
    }
)


def make(state: State, http: httpx.AsyncClient, now: list[datetime.datetime], dry_run: bool = False) -> batch.Batch:
    sched = scheduler.Scheduler(state, GitHub(http, "ghs_token"), dry_run=dry_run, clock=lambda: now[0])
    return batch.Batch(sched, pypi_fetch=WHEELS)


def live(state: State, monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    monkeypatch.setattr(state, "settings", state.settings.model_copy(update={"batch_live": names}))


def run(
    state: State,
    repo: str,
    run_id: int,
    workflow: str,
    status: str = "completed",
    conclusion: str | None = "success",
    created: datetime.datetime = SLOT,
    updated: datetime.datetime | None = None,
    event: str = "workflow_dispatch",
    branch: str = "main",
) -> Run:
    data = {
        "id": run_id,
        "name": workflow,
        "event": event,
        "head_branch": branch,
        "head_sha": "a" * 40,
        "run_number": run_id % 1000,
        "status": status,
        "conclusion": conclusion if status == "completed" else None,
        "run_started_at": created.isoformat(),
        "html_url": f"https://github.com/{ORG}/{repo}/actions/runs/{run_id}",
        "created_at": created.isoformat(),
        "updated_at": (updated or created + datetime.timedelta(minutes=30)).isoformat(),
        "run_attempt": 1,
    }
    row = Run.from_github(f"{ORG}/{repo}", data)
    state.model.upsert_run(row)
    return row


def job(
    state: State,
    repo: str,
    run_id: int,
    job_id: int,
    name: str,
    status: str = "completed",
    conclusion: str | None = "success",
    at: datetime.datetime = SLOT,
) -> None:
    data = {
        "id": job_id,
        "run_id": run_id,
        "name": name,
        "status": status,
        "conclusion": conclusion if status == "completed" else None,
        "created_at": at.isoformat(),
        "started_at": at.isoformat() if status != "queued" else None,
        "completed_at": (at + datetime.timedelta(minutes=5)).isoformat() if status == "completed" else None,
        "html_url": f"https://github.com/{ORG}/{repo}/actions/runs/{run_id}/job/{job_id}",
        "labels": [],
        "steps": [],
    }
    state.model.upsert_job(Job.from_github(f"{ORG}/{repo}", data))


# -- slots --------------------------------------------------------------------------------------------------------


def test_slots_keep_the_old_crons_apart() -> None:
    assert SELKIES.schedule == "daily at 04:30 UTC" and PROOT.schedule == "daily at 00:00 UTC"
    assert WHEELIE.schedule == "hourly at :20"
    assert SELKIES.last_slot(SLOT) == SLOT and SELKIES.next_slot(SLOT) == SLOT + batch.DAY
    before = SLOT - datetime.timedelta(minutes=1)
    assert SELKIES.last_slot(before) == SLOT - batch.DAY
    assert WHEELIE.last_slot(SLOT) == SLOT.replace(hour=4, minute=20)
    assert WHEELIE.last_slot(SLOT.replace(minute=10)) == SLOT.replace(hour=3, minute=20)
    assert SELKIES.defer_window == datetime.timedelta(hours=3) and WHEELIE.defer_window == datetime.timedelta(
        minutes=30
    )


# -- sizing -------------------------------------------------------------------------------------------------------


def test_capacity_counts_running_and_waiting_but_never_held_jobs(state: State) -> None:
    now = SLOT + datetime.timedelta(minutes=5)
    # Three image jobs running and one waiting for a runner.
    run(state, "docker-sonarr", 1, "Build", status="in_progress")
    for n in range(3):
        job(state, "docker-sonarr", 1, 10 + n, f"build {n}", status="in_progress")
    job(state, "docker-sonarr", 1, 13, "publish", status="queued")
    # proot-apps runs 4 jobs and holds 100 back with max-parallel: those are not demand.
    run(state, "proot-apps", 2, "Daily Package check", status="in_progress", branch="master")
    for n in range(4):
        job(state, "proot-apps", 2, 100 + n, f"app{n}", status="in_progress")
    for n in range(100):
        job(state, "proot-apps", 2, 200 + n, f"held{n}", status="queued")
    # A job whose completion webhook never came, from a finished run: not counted.
    run(state, "docker-radarr", 3, "Build", conclusion="success")
    job(state, "docker-radarr", 3, 400, "lost", status="in_progress")

    cap = batch.capacity(state, now, exclude="selkies-layers")
    assert (cap.running, cap.waiting, cap.held) == (3, 1, 100)
    assert cap.batch == {"proot-apps": 4}  # a cron run: the workflow default 4, or its jobs, whichever is more
    assert cap.free == 20 - 4 - 3 - 1 - 4 == 8
    assert cap.width(10) == 8 and cap.width(5) == 5 and not cap.busy

    # The same run started by control at 12 holds 12, even while it runs fewer jobs right now.
    state.model.add_decision(
        "proot-apps",
        "master",
        "dispatch",
        json.dumps({"parallel": 12}),
        "batch",
        (SLOT - datetime.timedelta(minutes=1)).isoformat(),
    )
    cap = batch.capacity(state, now, exclude="selkies-layers")
    assert cap.batch == {"proot-apps": 12} and cap.free == 0 and cap.busy
    assert cap.width(10) == 2  # never below the minimum

    # A push run after that dispatch was not started by control: it holds the workflow default, not 12 (2026-10-30,
    # a proot-apps push building 29 apps 4 wide was counted as 12 and left the fleet "busy" at 10 jobs).
    state.model.delete_run(2)
    run(state, "proot-apps", 4, "build-on-commit", status="in_progress", branch="master", event="push")
    for n in range(2):
        job(state, "proot-apps", 4, 500 + n, f"base{n}", status="in_progress")
    later = SLOT + datetime.timedelta(minutes=20)  # the dispatch is no longer pending either
    cap = batch.capacity(state, later, exclude="selkies-layers")
    assert cap.batch == {"proot-apps": 4} and cap.free == 8


def test_capacity_holds_a_fresh_dispatch_before_its_run_appears(state: State) -> None:
    state.model.add_decision(
        "selkies-layers", "main", "dispatch", json.dumps({"parallel": 9}), "batch", SLOT.isoformat()
    )
    assert batch.capacity(state, SLOT + datetime.timedelta(minutes=2)).batch == {"selkies-layers": 9}
    assert batch.capacity(state, SLOT + datetime.timedelta(minutes=20)).batch == {}  # no run came; reported apart
    # A dry run holds nothing.
    state.model.add_decision(
        "proot-apps", "master", "dispatch", json.dumps({"parallel": 9, "dry_run": True}), "batch", SLOT.isoformat()
    )
    assert "proot-apps" not in batch.capacity(state, SLOT + datetime.timedelta(minutes=2)).batch


# -- the schedule -------------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_live_project_is_dispatched_with_the_free_width(state: State, monkeypatch: pytest.MonkeyPatch) -> None:
    live(state, monkeypatch, "selkies-layers")
    run(state, "docker-sonarr", 1, "Build", status="in_progress")
    for n in range(4):
        job(state, "docker-sonarr", 1, 10 + n, f"build {n}", status="in_progress")
    github = FakeGitHub()
    now = [SLOT + datetime.timedelta(minutes=1)]
    async with mock_client(github) as http:
        runner = make(state, http, now)
        assert runner.due(SELKIES, now[0])
        decision = await runner.check(SELKIES)
        assert not runner.due(SELKIES, now[0])  # recorded: not again today
    assert decision.outcome == "dispatch" and decision.detail["parallel"] == 12  # 20-4-4
    assert decision.detail["reason"] == "scheduled" and decision.detail["capacity"]["running"] == 4
    assert github.posts == [
        (
            f"/repos/{ORG}/selkies-layers/actions/workflows/build.yml/dispatches",
            {"ref": "main", "inputs": {"parallel": "12"}},
        )
    ]
    assert batch.capacity(state, now[0]).batch == {"selkies-layers": 12}  # held while its run starts


@pytest.mark.anyio
async def test_projects_not_live_are_dry_runs(state: State, monkeypatch: pytest.MonkeyPatch) -> None:
    live(state, monkeypatch, "wheelie")
    github = FakeGitHub()
    now = [SLOT + datetime.timedelta(minutes=1)]
    async with mock_client(github) as http:
        decision = await make(state, http, now).check(SELKIES)
    assert decision.outcome == "dispatch" and decision.detail["dry_run"] is True and decision.id is not None
    assert decision.detail["live"] is False and decision.detail["parallel"] == 16 and github.posts == []
    assert batch.capacity(state, now[0]).batch == {}


@pytest.mark.anyio
async def test_busy_fleet_defers_then_starts_at_the_minimum(state: State, monkeypatch: pytest.MonkeyPatch) -> None:
    live(state, monkeypatch, "selkies-layers")
    run(state, "docker-webtop", 1, "Build", status="in_progress")
    for n in range(15):
        job(state, "docker-webtop", 1, 10 + n, f"build {n}", status="in_progress")
    github = FakeGitHub()
    now = [SLOT + datetime.timedelta(minutes=1)]
    async with mock_client(github) as http:
        runner = make(state, http, now)
        first = await runner.check(SELKIES)
        assert first.outcome == "skipped" and first.detail["reason"] == "fleet busy" and first.detail["deferred"]
        assert not runner.due(SELKIES, now[0] + datetime.timedelta(minutes=9))
        assert runner.due(SELKIES, now[0] + datetime.timedelta(minutes=10))
        now[0] = SLOT + datetime.timedelta(hours=2, minutes=55)  # the window closes before another retry
        last = await runner.check(SELKIES)
        assert not runner.due(SELKIES, now[0] + datetime.timedelta(hours=1))
    assert last.outcome == "dispatch" and last.detail["parallel"] == 2
    assert last.detail["reason"] == "fleet busy at the end of the window: started at the minimum"
    assert github.discord == []  # a deferral is not news
    assert len(github.posts) == 1


@pytest.mark.anyio
async def test_previous_run_still_going_defers_then_skips_the_slot(state: State) -> None:
    run(state, "selkies-layers", 5, "Build Layers", status="in_progress", created=SLOT - datetime.timedelta(hours=2))
    now = [SLOT + datetime.timedelta(minutes=1)]
    async with mock_client(FakeGitHub()) as http:
        runner = make(state, http, now)
        first = await runner.check(SELKIES)
        now[0] = SLOT + datetime.timedelta(hours=3)
        last = await runner.check(SELKIES)
    assert first.outcome == "skipped" and first.detail["deferred"] and first.detail["run_id"] == 5
    assert last.outcome == "skipped" and last.detail["reason"] == "previous run in progress: slot skipped"
    assert "retry_at" not in last.detail


@pytest.mark.anyio
async def test_a_missed_slot_is_not_run_late(state: State) -> None:
    async with mock_client(FakeGitHub()) as http:
        runner = make(state, http, [SLOT])
        assert runner.due(SELKIES, SLOT + datetime.timedelta(hours=2, minutes=59))
        assert not runner.due(SELKIES, SLOT + datetime.timedelta(hours=3, minutes=1))
        assert not runner.due(PROOT, SLOT)  # 00:00 + 4h30
        assert runner.due(WHEELIE, SLOT.replace(minute=50)) and not runner.due(WHEELIE, SLOT.replace(minute=51))


# -- wheelie ------------------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_wheelie_without_gaps_dispatches_nothing(state: State) -> None:
    github = FakeGitHub(packages="cffi\n")
    now = [SLOT.replace(minute=21)]
    async with mock_client(github) as http:
        decision = await make(state, http, now).check(WHEELIE)
    assert decision.outcome == "unchanged" and decision.detail["gaps"] == []
    assert decision.detail["reason"] == "no gaps: PyPI or the index has every wheel" and github.posts == []


@pytest.mark.anyio
async def test_wheelie_dispatches_one_leg_per_gap(state: State) -> None:
    github = FakeGitHub()  # bluepy has no wheel anywhere
    now = [SLOT.replace(minute=21)]
    async with mock_client(github) as http:
        decision = await make(state, http, now).check(WHEELIE)
    assert decision.outcome == "dispatch" and decision.detail["parallel"] == 2  # two legs, whatever is free
    assert decision.detail["gaps"] == [
        {"distro": "alpine-3.24", "arch": "amd64", "packages": ["bluepy==1.3.0"]},
        {"distro": "alpine-3.24", "arch": "arm64", "packages": ["bluepy==1.3.0"]},
    ]
    assert github.posts == [
        (f"/repos/{ORG}/wheelie/actions/workflows/build.yml/dispatches", {"ref": "main", "inputs": {"parallel": "2"}})
    ]
    # Our index already has it: no gap.
    built = "".join(f'<a href="bluepy-1.3.0-cp314-cp314-linux_{arch}.whl">w</a>' for arch in ("x86_64", "aarch64"))
    async with mock_client(FakeGitHub(index=built)) as http:
        again = await make(state, http, [now[0] + datetime.timedelta(hours=1)]).check(WHEELIE)
    assert again.outcome == "unchanged"


@pytest.mark.anyio
async def test_wheelie_waits_a_day_after_the_same_gaps_failed(state: State) -> None:
    github = FakeGitHub()
    now = [SLOT.replace(minute=21)]
    async with mock_client(github) as http:
        runner = make(state, http, now)
        first = await runner.check(WHEELIE)
        run(state, "wheelie", 9, "Build", conclusion="failure", created=now[0] + datetime.timedelta(seconds=20))
        now[0] += batch.HOUR
        held = await runner.check(WHEELIE)
        now[0] += batch.DAY
        again = await runner.check(WHEELIE)
    assert first.outcome == "dispatch" and again.outcome == "dispatch"
    assert held.outcome == "skipped" and held.detail["reason"] == "the same gaps failed to build: tried again daily"
    assert held.detail["run_id"] == 9 and len(github.posts) == 2


@pytest.mark.anyio
async def test_dispatch_refused_alerts_once(state: State, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(state.discord, "webhook_url", "https://discord.com/api/webhooks/1/t")
    live(state, monkeypatch, "selkies-layers")
    github = FakeGitHub(status=403)
    now = [SLOT + datetime.timedelta(minutes=1)]
    async with mock_client(github) as http:
        state.discord.client = http
        runner = make(state, http, now)
        first = await runner.check(SELKIES)
        now[0] += datetime.timedelta(minutes=10)
        assert runner.due(SELKIES, now[0])
        second = await runner.check(SELKIES)
    assert first.outcome == second.outcome == "error" and first.detail["stage"] == "dispatch"
    assert first.detail["message"].startswith("HTTP 403: Resource not accessible by integration (does the GitHub App")
    assert len(github.discord) == 1
    (alert,) = github.discord[0]
    assert alert["title"] == "selkies-layers · \U0001f534 dispatch failed" and alert["color"] == events.RED
    assert "Control could not start `build.yml` on `main`" in alert["description"]


@pytest.mark.anyio
async def test_gap_check_errors_alert_after_a_streak(state: State, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(state.discord, "webhook_url", "https://discord.com/api/webhooks/1/t")
    github = FakeGitHub()
    del github.files["distros.json"]
    async with mock_client(github) as http:
        state.discord.client = http
        for hour in range(batch.GAP_ALERT_AFTER + 1):
            now = [SLOT.replace(minute=21) + datetime.timedelta(hours=hour)]
            decision = await make(state, http, now).check(WHEELIE)
            assert decision.outcome == "error" and decision.detail["stage"] == "gap check"
    assert len(github.discord) == 1 and github.discord[0][0]["title"] == "wheelie · \U0001f534 gap check failing"


# -- watching -----------------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_proot_app_fixed_by_a_push_clears_its_package_check_failure(
    state: State, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(state.discord, "webhook_url", "https://discord.com/api/webhooks/1/t")
    repo = "proot-apps"
    run(state, repo, 1, "Daily Package check", conclusion="failure", created=SLOT, branch="master")
    job(state, repo, 1, 11, "solidcraft", conclusion="failure")
    job(state, repo, 1, 12, "firefox")
    github = FakeGitHub()
    now = [SLOT + datetime.timedelta(hours=1)]
    async with mock_client(github) as http:
        state.discord.client = http
        runner = make(state, http, now)
        await runner.watch()
        assert [u.unit for u in state.model.batch_units(f"{ORG}/{repo}", "fail")] == ["solidcraft"]
        # The fix is pushed; build-on-commit builds solidcraft (and only it) green.
        run(state, repo, 2, "build-on-commit", created=SLOT + datetime.timedelta(hours=2), branch="master")
        job(state, repo, 2, 21, "solidcraft")
        job(state, repo, 2, 22, "Ingest Metadata")
        now[0] = SLOT + datetime.timedelta(hours=3)
        await runner.watch()
    assert state.model.batch_units(f"{ORG}/{repo}", "fail") == []
    cleared = state.model.batch_unit(f"{ORG}/{repo}", "Daily Package check", "solidcraft")
    assert cleared and cleared.run_id == 2
    recovered = github.discord[-1][0]
    assert recovered["title"] == "proot-apps · \U0001f7e2 recovered" and "`solidcraft`" in recovered["description"]


@pytest.mark.anyio
async def test_a_failing_unit_its_workflow_stopped_building_is_retired(
    state: State, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = "proot-apps"
    run(state, repo, 1, "Daily Package check", conclusion="failure", created=SLOT, branch="master")
    job(state, repo, 1, 11, "oldapp", conclusion="failure")
    job(state, repo, 1, 12, "firefox", conclusion="failure")
    github = FakeGitHub()
    now = [SLOT + datetime.timedelta(hours=1)]
    async with mock_client(github) as http:
        runner = make(state, http, now)
        await runner.watch()
        # oldapp is disabled; every night after builds the rest, and firefox keeps failing.
        for night in range(1, 5):
            created = SLOT + night * batch.DAY
            run(state, repo, 1 + night, "Daily Package check", conclusion="failure", created=created, branch="master")
            job(state, repo, 1 + night, 100 + night, "firefox", conclusion="failure")
            now[0] = created + datetime.timedelta(hours=1)
            await runner.watch()
            failing = {u.unit for u in state.model.batch_units(f"{ORG}/{repo}", "fail")}
            # Retired once a run finished more than RETIRE_AFTER after the last one that built it.
            assert failing == ({"firefox", "oldapp"} if night <= 2 else {"firefox"})


@pytest.mark.anyio
async def test_units_alert_when_they_start_failing_and_once_when_they_recover(
    state: State, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(state.discord, "webhook_url", "https://discord.com/api/webhooks/1/t")
    repo = "selkies-layers"
    # A run from before control watched: learnt silently.
    run(state, repo, 1, "Build Layers", conclusion="failure", created=SLOT - batch.DAY)
    job(state, repo, 1, 11, "ubuntu-noble-xfce-amd64", conclusion="failure")
    job(state, repo, 1, 12, "debian-trixie-kde-arm64")
    job(state, repo, 1, 13, "Generate matrix")
    github = FakeGitHub()
    now = [SLOT]
    async with mock_client(github) as http:
        state.discord.client = http
        runner = make(state, http, now)
        await runner.watch()
        assert github.discord == []
        # Night two: kde breaks, xfce stays red.
        run(state, repo, 2, "Build Layers", conclusion="failure", created=SLOT + datetime.timedelta(minutes=1))
        job(state, repo, 2, 21, "ubuntu-noble-xfce-amd64", conclusion="failure")
        job(state, repo, 2, 22, "debian-trixie-kde-arm64", conclusion="failure")
        job(state, repo, 2, 23, "fedora-43-i3-amd64", conclusion="cancelled")
        now[0] = SLOT + datetime.timedelta(hours=1)
        await runner.watch()
        assert len(github.discord) == 1
        (failing,) = github.discord[0]
        # Night three: still red, nothing to say. Night four: both pass.
        run(state, repo, 3, "Build Layers", conclusion="failure", created=SLOT + batch.DAY)
        job(state, repo, 3, 31, "debian-trixie-kde-arm64", conclusion="failure")
        now[0] = SLOT + batch.DAY + datetime.timedelta(hours=1)
        await runner.watch()
        assert len(github.discord) == 1
        run(state, repo, 4, "Build Layers", created=SLOT + 2 * batch.DAY)
        job(state, repo, 4, 41, "debian-trixie-kde-arm64")
        job(state, repo, 4, 42, "ubuntu-noble-xfce-amd64")
        now[0] = SLOT + 2 * batch.DAY + datetime.timedelta(hours=1)
        await runner.watch()
        await runner.watch()  # evaluated once
    assert failing["title"] == "selkies-layers · \U0001f534 debian-trixie-kde-arm64 failing"
    assert failing["color"] == events.RED and failing["url"] == f"https://github.com/{ORG}/{repo}/actions/runs/2"
    assert failing["description"] == (
        f"[Build Layers #2](https://github.com/{ORG}/{repo}/actions/runs/2) failure: 1 newly failing, "
        "1 still failing from before (not repeated).\n"
        f"- [`debian-trixie-kde-arm64`](https://github.com/{ORG}/{repo}/actions/runs/2/job/22)"
    )
    assert len(github.discord) == 2
    (recovered,) = github.discord[1]
    assert recovered["title"] == "selkies-layers · \U0001f7e2 recovered" and recovered["color"] == events.GREEN
    assert "`debian-trixie-kde-arm64`" in recovered["description"]
    assert "`ubuntu-noble-xfce-amd64`" in recovered["description"]
    assert state.model.batch_units(f"{ORG}/{repo}", "fail") == []
    runs = state.model.batch_runs(f"{ORG}/{repo}")
    assert [(r.run_id, r.alerted, r.newly_failing) for r in runs] == [
        (4, True, []),
        (3, False, []),
        (2, True, ["debian-trixie-kde-arm64"]),
        (1, False, ["ubuntu-noble-xfce-amd64"]),
    ]


@pytest.mark.anyio
async def test_wheelie_failures_name_the_packages(state: State, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(state.discord, "webhook_url", "https://discord.com/api/webhooks/1/t")
    state.db.set(batch.WATCH_SINCE, (SLOT - batch.DAY).isoformat())
    gaps = [{"distro": "alpine-3.24", "arch": "arm64", "packages": ["bluepy==1.3.0"]}]
    state.model.add_decision(
        "wheelie", "main", "dispatch", json.dumps({"parallel": 1, "gaps": gaps}), "batch", SLOT.isoformat()
    )
    run(state, "wheelie", 7, "Build", conclusion="failure", created=SLOT + datetime.timedelta(seconds=30))
    job(state, "wheelie", 7, 71, "Gap check")
    job(state, "wheelie", 7, 72, "alpine-3.24 arm64", conclusion="failure")
    job(state, "wheelie", 7, 73, "Publish", conclusion="skipped")
    github = FakeGitHub()
    async with mock_client(github) as http:
        state.discord.client = http
        await make(state, http, [SLOT + datetime.timedelta(hours=1)]).watch()
    (embed,) = github.discord[0]
    assert "[`alpine-3.24 arm64`](" in embed["description"] and "(bluepy==1.3.0)" in embed["description"]


@pytest.mark.anyio
async def test_a_run_that_failed_before_any_job_is_one_unit(state: State, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(state.discord, "webhook_url", "https://discord.com/api/webhooks/1/t")
    state.db.set(batch.WATCH_SINCE, (SLOT - batch.DAY).isoformat())
    run(state, "proot-apps", 8, "Daily Package check", conclusion="startup_failure", branch="master")
    github = FakeGitHub()
    async with mock_client(github) as http:
        state.discord.client = http
        runner = make(state, http, [SLOT + datetime.timedelta(minutes=5)])
        await runner.watch()
        assert github.discord == []  # no job yet: give the webhooks time
        runner.scheduler.clock = lambda: SLOT + datetime.timedelta(hours=1)
        await runner.watch()
    (embed,) = github.discord[0]
    assert embed["title"] == "proot-apps · \U0001f534 run failing"
    assert "`Daily Package check run`" in embed["description"]


@pytest.mark.anyio
async def test_dispatch_without_a_run_and_overdue_runs_alert_once(
    state: State, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(state.discord, "webhook_url", "https://discord.com/api/webhooks/1/t")
    state.model.add_decision(
        "selkies-layers", "main", "dispatch", json.dumps({"parallel": 8}), "batch", SLOT.isoformat()
    )
    # proot-apps: three past runs of about an hour, and one going for five.
    for n in range(3):
        start = SLOT - batch.DAY * (n + 1)
        run(
            state,
            "proot-apps",
            50 + n,
            "Daily Package check",
            created=start,
            updated=start + batch.HOUR,
            branch="master",
        )
    run(
        state,
        "proot-apps",
        60,
        "Daily Package check",
        status="in_progress",
        created=SLOT - datetime.timedelta(hours=5),
        branch="master",
    )
    github = FakeGitHub()
    async with mock_client(github) as http:
        state.discord.client = http
        runner = make(state, http, [SLOT + datetime.timedelta(minutes=20)])
        await runner.watch()
        await runner.watch()
    titles = sorted(embeds[0]["title"] for embeds in github.discord)
    assert titles == [
        "proot-apps · \U0001f7e1 run overdue",
        "selkies-layers · \U0001f534 dispatched run never appeared",
    ]


def test_overdue_threshold() -> None:
    assert batch.overdue_after([]) == batch.OVERDUE_UNKNOWN
    assert batch.overdue_after([600.0] * 5) == batch.OVERDUE_FLOOR
    assert batch.overdue_after([4 * 3600.0] * 5) == datetime.timedelta(hours=8)


# -- the page -----------------------------------------------------------------------------------------------------


def test_batch_page_and_json(client: TestClient, state: State, monkeypatch: pytest.MonkeyPatch) -> None:
    live(state, monkeypatch, "wheelie")
    state.model.add_decision(
        "selkies-layers",
        "main",
        "dispatch",
        json.dumps({"parallel": 6, "dry_run": True, "reason": "scheduled"}),
        "batch",
    )
    body = client.get("/api/batch.json").json()
    assert [p["repo"] for p in body["projects"]] == ["proot-apps", "selkies-layers", "wheelie"]
    selkies = body["projects"][1]
    assert selkies["live"] is False and selkies["schedule"] == "daily at 04:30 UTC" and selkies["parallel_now"] == 16
    assert selkies["last_dispatch"]["info"]["parallel"] == 6 and selkies["last_dispatch"]["run"] is None
    assert body["projects"][2]["live"] is True  # wheelie has no cron of its own
    assert body["capacity"]["free"] == 16 and body["capacity"]["reserve"] == 4
    assert len(body["decisions"]) == 1
    page = client.get("/batch")
    assert page.status_code == 200 and "selkies-layers" in page.text and "dry run" in page.text
    assert "batch" in client.get("/api/scheduler.json").json()["kinds"]


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from control.config import Settings

    monkeypatch.setenv("CONTROL_BATCH_LIVE", "wheelie, proot-apps")
    monkeypatch.setenv("CONTROL_BATCH_RESERVE", "2")
    settings = Settings.from_env()
    assert settings.batch_live == ("wheelie", "proot-apps") and settings.batch_reserve == 2
    monkeypatch.setenv("CONTROL_BATCH_LIVE", "")
    assert Settings.from_env().batch_live == ()
