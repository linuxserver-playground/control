import datetime
import json
import os
from typing import Any

import httpx
import pytest

from control import scheduler
from control.__main__ import main
from control.fleet import manifest_row
from control.github import GitHub
from control.model import Release, Repo, SchedulerDecision
from control.state import State
from control.vendor import resolve_version as rv

from .conftest import REPO, fixture, mock_client, seed

NOW = datetime.datetime(2026, 10, 24, 13, 0, tzinfo=datetime.UTC)
MANIFEST: dict[str, Any] = {
    "name": "sonarr",
    "upstream": {"version": {"type": "json", "url": "https://services.sonarr.tv/v1/releases", "jq": ".[0]"}},
    "release": {"branch": "main", "tag": "latest", "channel": "stable"},
    "build": {"platforms": ["amd64", "arm64"]},
}


class FakeDispatch:
    """GitHub's dispatch endpoints: records every POST and answers 204."""

    def __init__(self, status: int = 204) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.status = status
        self.discord: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "discord.com":
            self.discord.append(json.loads(request.content)["content"])
            return httpx.Response(200, json={"id": "1"})
        assert request.method == "POST" and request.headers["Authorization"] == "Bearer ghs_token"
        self.posts.append((request.url.path, json.loads(request.content)))
        return httpx.Response(self.status, json={} if self.status != 204 else None)


class FakeResolver:
    def __init__(self, upstream: str | None = "4.0.20.3014", error: Exception | None = None) -> None:
        self.upstream = upstream
        self.error = error
        self.calls: list[tuple[dict[str, Any], str | None]] = []

    async def __call__(self, manifest: dict[str, Any], token: str | None) -> dict[str, Any]:
        self.calls.append((manifest, token))
        if self.error:
            raise self.error
        return {"upstream": self.upstream, "upstream_clean": rv.sanitize(self.upstream or ""), "source_url": "x"}


def live(state: State, branch: str = "main", data: dict[str, Any] | None = None, repo: str = REPO) -> Any:
    """A repo row and a manifests row, as discovery would leave them."""
    state.model.upsert_repo(
        Repo.from_github({**fixture("api.repos")[0], "full_name": repo, "name": repo.split("/")[-1]})
    )
    manifest = data or MANIFEST
    if branch != "main":
        manifest = {**manifest, "release": {"branch": branch, "tag": branch, "channel": "prerelease"}}
    row = manifest_row(repo, branch, f"sha-{branch}", manifest)
    state.model.upsert_manifest(row)
    return row


async def run_check(
    state: State, manifest: Any, resolver: FakeResolver, github: FakeDispatch | None = None, **kwargs: Any
) -> tuple[scheduler.Decision, FakeDispatch]:
    github = github or FakeDispatch()
    async with mock_client(github) as http:
        state.discord.client = http
        sched = scheduler.Scheduler(state, GitHub(http, "ghs_token"), resolver=resolver, **kwargs)
        return await sched.check_upstream(manifest), github


def decisions(state: State) -> list[SchedulerDecision]:
    return list(reversed(state.model.recent_decisions(50)))


# -- spreading ----------------------------------------------------------------------------------------------------


def test_slots_are_deterministic_and_spread() -> None:
    assert scheduler.slot(REPO, "main") == scheduler.slot(REPO, "main")
    minutes = {scheduler.slot(f"example-org/docker-{n}", "main")[0] for n in range(300)}
    hours = {scheduler.slot(f"example-org/docker-{n}", "main")[1] for n in range(300)}
    assert len(minutes) > 50 and all(0 <= m < 60 for m in minutes)
    assert len(hours) > 100 and all(0 <= h < 168 for h in hours)
    assert scheduler.slot(REPO, "main") != scheduler.slot(REPO, "develop")


def test_upstream_slots_hourly() -> None:
    minute, _ = scheduler.slot(REPO, "main")
    last = scheduler.last_upstream_slot(REPO, "main", NOW)
    nxt = scheduler.next_upstream_check(REPO, "main", NOW)
    assert last.minute == minute and nxt - last == datetime.timedelta(hours=1) and last <= NOW < nxt
    just_before = last - datetime.timedelta(seconds=1)
    assert scheduler.next_upstream_check(REPO, "main", just_before) == last
    # Due once per slot: a record from before the slot means due, one after it means not.
    assert scheduler.due(last, None)
    assert scheduler.due(last, (last - datetime.timedelta(minutes=1)).isoformat())
    assert not scheduler.due(last, last.isoformat())


def test_package_slots_weekly() -> None:
    minute, hour = scheduler.slot(REPO, "main")
    last = scheduler.last_package_slot(REPO, "main", NOW)
    nxt = scheduler.next_package_check(REPO, "main", NOW)
    assert nxt - last == datetime.timedelta(weeks=1) and last <= NOW < nxt
    assert last.minute == minute and (last.weekday() * 24 + last.hour) == hour


# -- released versions --------------------------------------------------------------------------------------------


def test_released_from_tag_follows_next_tag_rules() -> None:
    assert scheduler.released_from_tag("4.0.20.3014-ls1", "latest", ["develop"]) == "4.0.20.3014"
    assert scheduler.released_from_tag("develop-5.0.1.1234-ls7", "latest", ["develop"]) is None
    assert scheduler.released_from_tag("develop-5.0.1.1234-ls7", "develop", ["latest"]) == "5.0.1.1234"
    assert scheduler.released_from_tag("5.0.1.1234-ls7", "develop", ["latest"]) is None
    assert scheduler.released_from_tag("v1.2.3", "latest", []) is None
    assert scheduler.released_from_tag("2024.01-r1-ls12", "latest", []) == "2024.01-r1"


def test_released_version_picks_newest_of_branch() -> None:
    def release(tag: str, when: str) -> Release:
        return Release(
            repo=REPO,
            tag=tag,
            name=tag,
            prerelease=False,
            published_at=when,
            html_url=None,
            body=None,
            created_at=when,
            updated_at=when,
        )

    releases = [
        release("develop-5.0.2-ls8", "2026-10-24T12:00:00+00:00"),
        release("4.0.20-ls3", "2026-10-23T12:00:00+00:00"),
    ]
    assert scheduler.released_version(releases, "main", "latest", ["develop"]) == ("4.0.20", "4.0.20-ls3")
    assert scheduler.released_version(releases, "develop", "develop", ["latest"]) == ("5.0.2", "develop-5.0.2-ls8")
    assert scheduler.released_version([], "main", "latest", []) == (None, None)


# -- decisions ----------------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_unchanged_when_release_matches_upstream(state: State) -> None:
    seed(state, "release.published")  # 4.0.20.3014-ls1
    manifest = live(state)
    resolver = FakeResolver("4.0.20.3014")
    decision, github = await run_check(state, manifest, resolver)
    assert decision.outcome == "unchanged" and github.posts == []
    assert decision.detail["upstream"] == "4.0.20.3014" and decision.detail["released_tag"] == "4.0.20.3014-ls1"
    assert resolver.calls[0][1] == "ghs_token"  # the installation token goes to the resolver
    assert [d.outcome for d in decisions(state)] == ["unchanged"]
    assert decisions(state)[0].kind == "upstream" and decisions(state)[0].info["released"] == "4.0.20.3014"


@pytest.mark.anyio
async def test_dispatch_when_upstream_moves(state: State) -> None:
    seed(state, "release.published")
    manifest = live(state)
    decision, github = await run_check(state, manifest, FakeResolver("v4.0.21.0"))
    assert decision.outcome == "dispatch" and decision.id is not None
    assert github.posts == [
        (f"/repos/{REPO}/actions/workflows/build.yml/dispatches", {"ref": "main", "inputs": {"reason": "upstream"}})
    ]
    assert decision.detail["upstream"] == "v4.0.21.0" and decision.detail["released"] == "4.0.20.3014"
    assert decision.detail["reason"] == "upstream changed" and decision.detail["dispatch_reason"] == "upstream"

    # Straight after, the same branch is in cooldown even though upstream still differs.
    again, github = await run_check(state, manifest, FakeResolver("v4.0.21.0"))
    assert again.outcome == "skipped" and again.detail["reason"] == "cooldown" and github.posts == []
    assert again.detail["dispatched_at"] == decision_at(state, decision)


def decision_at(state: State, decision: scheduler.Decision) -> str:
    assert decision.id is not None
    row = state.model.decision(decision.id)
    assert row
    return row.at


@pytest.mark.anyio
async def test_dispatch_when_nothing_released_yet(state: State) -> None:
    manifest = live(state)
    decision, github = await run_check(state, manifest, FakeResolver("1.0"))
    assert decision.outcome == "dispatch" and decision.detail["reason"] == "no release yet"
    assert decision.detail["released"] is None and len(github.posts) == 1


@pytest.mark.anyio
async def test_build_summary_is_the_hint_without_releases(state: State) -> None:
    seed(state, "control.build.summary")  # upstream 4.0.20.3014 on main, no Release row
    manifest = live(state)
    decision, _ = await run_check(state, manifest, FakeResolver("4.0.20.3014"))
    assert decision.outcome == "unchanged" and decision.detail["released_tag"] == "4.0.20.3014-ls1"


@pytest.mark.anyio
async def test_non_default_branch_reads_its_prefixed_release(state: State) -> None:
    live(state)
    develop = live(state, "develop")
    release = fixture("release.published")
    release["release"].update(tag_name="develop-5.0.1.1234-ls7", published_at="2026-10-24T13:00:00Z")
    from control import handlers

    handlers.process(state.model, state.stream, "release", release)
    seed(state, "release.published")  # the default branch's 4.0.20.3014-ls1, published earlier
    decision, github = await run_check(state, develop, FakeResolver("5.0.1.1234"))
    assert decision.outcome == "unchanged" and decision.detail["released_tag"] == "develop-5.0.1.1234-ls7"
    decision, github = await run_check(state, develop, FakeResolver("5.0.2.0"))
    assert decision.outcome == "dispatch"
    assert github.posts[0][1] == {"ref": "develop", "inputs": {"reason": "upstream"}}


@pytest.mark.anyio
async def test_skipped_outcomes(state: State) -> None:
    seed(state, "release.published")
    manifest = live(state)

    # An image-wide flag stops everything before the resolver runs.
    state.model.set_skip("sonarr", "broken upstream", "octocat")
    resolver = FakeResolver("9.9")
    decision, github = await run_check(state, manifest, resolver)
    assert decision.outcome == "skipped" and decision.detail == {
        "reason": "skip",
        "scope": "sonarr",
        "note": "broken upstream",
    }
    assert resolver.calls == [] and github.posts == []
    state.model.remove_skip("sonarr")

    # A branch flag too; a flag on another branch does not.
    state.model.set_skip("sonarr/develop", None, "octocat")
    decision, _ = await run_check(state, manifest, FakeResolver("9.9"))
    assert decision.outcome == "dispatch"
    state.model.set_skip("sonarr/main", None, "octocat")
    decision, _ = await run_check(state, manifest, FakeResolver("9.9"))
    assert decision.outcome == "skipped" and decision.detail["scope"] == "sonarr/main"
    state.model.remove_skip("sonarr/main")
    state.model.remove_skip("sonarr/develop")

    # A version flag only bites once the resolver names that version (on a branch not in cooldown).
    fresh = live(state, "nightly")
    state.model.set_skip("sonarr/nightly/10.0", "bad release", "octocat")
    decision, _ = await run_check(state, fresh, FakeResolver("10.0"))
    assert decision.outcome == "skipped" and decision.detail["scope"] == "sonarr/nightly/10.0"
    assert decision.detail["upstream"] == "10.0"
    decision, _ = await run_check(state, fresh, FakeResolver("10.1"))
    assert decision.outcome == "dispatch"

    # A run in flight on the branch.
    seed(state, "workflow_run.in_progress")  # run 5001 on main
    decision, _ = await run_check(state, manifest, FakeResolver("9.9"))
    assert decision.outcome == "skipped" and decision.detail["reason"] == "run in progress"
    assert decision.detail["run_id"] == 5001
    seed(state, "workflow_run.completed")

    # os images and sources control will not run.
    os_image = live(state, "os", {**MANIFEST, "upstream": {"version": {"type": "os"}}})
    decision, _ = await run_check(state, os_image, FakeResolver("x"))
    assert decision.outcome == "skipped" and "os image" in decision.detail["reason"]
    cmd = live(state, "cmd", {**MANIFEST, "upstream": {"version": {"type": "command", "run": "echo 1"}}})
    decision, _ = await run_check(state, cmd, FakeResolver("x"))
    assert decision.outcome == "skipped" and decision.detail["source"] == "command"

    # Assets not published yet is "try later", not an error.
    assets = live(state, "assets")
    decision, _ = await run_check(state, assets, FakeResolver(error=rv.AssetsNotReady("HEAD 404")))
    assert decision.outcome == "skipped" and decision.detail["reason"] == "assets not ready"


@pytest.mark.anyio
async def test_errors_alert_discord_once_after_six(state: State, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = live(state)
    monkeypatch.setattr(state.discord, "webhook_url", "https://discord.com/api/webhooks/1/t")
    github = FakeDispatch()
    for n in range(1, 8):
        decision, github = await run_check(state, manifest, FakeResolver(error=rv.ResolveError("HTTP 500")), github)
        assert decision.outcome == "error" and decision.detail["message"] == "ResolveError: HTTP 500"
        assert state.model.consecutive_errors("sonarr", "main") == n
    assert len(github.discord) == 1 and "failed 6 times in a row" in github.discord[0] and "sonarr" in github.discord[0]
    # A success resets the streak.
    decision, _ = await run_check(state, manifest, FakeResolver("1.0"))
    assert decision.outcome == "dispatch" and state.model.consecutive_errors("sonarr", "main") == 0


@pytest.mark.anyio
async def test_dispatch_failure_is_an_error_decision(state: State) -> None:
    manifest = live(state)
    decision, _ = await run_check(state, manifest, FakeResolver("1.0"), FakeDispatch(status=422))
    assert decision.outcome == "error" and "422" in decision.detail["message"]
    assert decision.detail["upstream"] == "1.0"


@pytest.mark.anyio
async def test_package_check(state: State) -> None:
    manifest = live(state)
    github = FakeDispatch()
    async with mock_client(github) as http:
        sched = scheduler.Scheduler(state, GitHub(http, "ghs_token"))
        decision = await sched.check_packages(manifest)
        assert decision.outcome == "dispatch" and decision.kind == "packages"
        assert github.posts == [
            (f"/repos/{REPO}/actions/workflows/build.yml/dispatches", {"ref": "main", "inputs": {"reason": "packages"}})
        ]
        seed(state, "workflow_run.in_progress")
        decision = await sched.check_packages(manifest)
        assert decision.outcome == "skipped" and decision.detail["reason"] == "run in progress"
        seed(state, "workflow_run.completed")
        state.model.set_skip("sonarr", None, "octocat")
        decision = await sched.check_packages(manifest)
        assert decision.outcome == "skipped" and decision.detail["scope"] == "sonarr"
    assert [d.kind for d in decisions(state)] == ["packages"] * 3


@pytest.mark.anyio
async def test_tick_runs_each_check_once_per_slot(state: State) -> None:
    manifest = live(state)
    seed(state, "release.published")
    state.db.set("last_discovery", json.dumps({"started": NOW.isoformat()}))  # discovery not due
    github = FakeDispatch()
    resolver = FakeResolver("4.0.20.3014")
    now = NOW
    async with mock_client(github) as http:
        sched = scheduler.Scheduler(state, GitHub(http, "ghs_token"), resolver=resolver, clock=lambda: now)
        first = await sched.tick()
        kinds = sorted(d.kind for d in first)
        assert kinds == ["packages", "upstream"]  # nothing recorded before: both schedules are due
        now = NOW + datetime.timedelta(minutes=1)
        assert await sched.tick() == []  # same slot, already done
        now = NOW + datetime.timedelta(hours=1, minutes=1)
        second = await sched.tick()
        assert [d.kind for d in second] == ["upstream"]  # a new hour, same week
        assert second[0].outcome == "skipped" and second[0].detail["reason"] == "cooldown"  # the package dispatch
        # A manifest that opts out of the package check only gets the upstream schedule.
        state.model.upsert_manifest(manifest.model_copy(update={"package_check": False}))
        state.model.upsert_manifest(live(state, "develop").model_copy(update={"package_check": False}))
        now = NOW + datetime.timedelta(weeks=1, hours=2)
        third = await sched.tick()
        assert sorted((d.branch, d.kind) for d in third) == [("develop", "upstream"), ("main", "upstream")]
    # One package dispatch for main, one upstream dispatch for develop (which has no release yet); main unchanged.
    assert len(resolver.calls) == 3
    assert [(p[1]["ref"], p[1]["inputs"]["reason"]) for p in github.posts] == [
        ("main", "packages"),
        ("develop", "upstream"),
    ]


@pytest.mark.anyio
async def test_dry_run_records_and_dispatches_nothing(state: State) -> None:
    manifest = live(state)
    decision, github = await run_check(state, manifest, FakeResolver("1.0"), dry_run=True)
    assert decision.outcome == "dispatch" and decision.id is None and decision.detail["dry_run"] is True
    assert github.posts == [] and state.model.recent_decisions() == []
    line = decision.line()
    assert line.startswith("sonarr/main [upstream] dispatch: upstream=1.0, ") and "dry_run=True" in line


@pytest.mark.anyio
async def test_run_once_filters_and_works_without_installation(state: State, monkeypatch: pytest.MonkeyPatch) -> None:
    live(state)
    live(state, "develop")
    resolver = FakeResolver("1.0")
    monkeypatch.setattr(scheduler, "resolve_in_thread", resolver)
    found = await scheduler.run_once(state, branch="develop", dry_run=True)
    assert [(d.image, d.branch, d.outcome) for d in found] == [("sonarr", "develop", "dispatch")]
    assert resolver.calls[0][1] is None  # no installation: no token for the resolver
    found = await scheduler.run_once(state, image="docker-sonarr")
    assert [d.branch for d in found] == ["develop", "main"]
    # Without the App there is nothing to dispatch with: the real run records errors, not silence.
    assert {d.outcome for d in found} == {"error"} and all(d.kind == "manual" for d in found)
    assert "not configured" in found[0].detail["message"]
    assert len(state.model.recent_decisions()) == 2


def test_cli_schedule_once_dry_run(
    state: State, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    live(state, "main", {**MANIFEST, "upstream": {"version": {"type": "os"}}})
    monkeypatch.setitem(os.environ, "CONTROL_DATA_DIR", str(state.settings.data_dir))
    monkeypatch.setitem(os.environ, "CONTROL_PUBLIC_URL", state.settings.public_url)
    assert main(["schedule", "--once", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("sonarr/main [manual] skipped: reason=os image")
    assert state.model.recent_decisions() == []
    assert main(["schedule", "--once", "--image", "nothing"]) == 0
    assert "no live branches matched" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["schedule"])  # --once is required


def test_upcoming_lists_next_checks(state: State) -> None:
    main_row = live(state)
    dev_row = live(state, "develop").model_copy(update={"package_check": False})
    rows = scheduler.upcoming([main_row, dev_row], NOW)
    kinds = {(r["branch"], r["kind"]) for r in rows}
    assert ("main", "upstream") in kinds and ("develop", "upstream") in kinds and ("develop", "packages") not in kinds
    assert rows == sorted(rows, key=lambda r: (r["at"], r["image"], r["branch"]))
    for row in rows:
        at = datetime.datetime.fromisoformat(str(row["at"]))
        assert NOW < at <= NOW + datetime.timedelta(days=7)
        if row["kind"] == "upstream":
            assert at - NOW <= datetime.timedelta(hours=1)
