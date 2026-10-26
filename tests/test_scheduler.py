import datetime
import json
import os
from typing import Any

import httpx
import pytest

from control import events, scheduler
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
        self.discord: list[dict[str, Any]] = []
        """Every embed posted to Discord."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "discord.com":
            body = json.loads(request.content)
            assert "content" not in body and len(body["embeds"]) == 1
            self.discord.extend(body["embeds"])
            return httpx.Response(200, json={"id": "1"})
        assert request.method == "POST" and request.headers["Authorization"] == "Bearer ghs_token"
        self.posts.append((request.url.path, json.loads(request.content)))
        return httpx.Response(self.status, json={} if self.status != 204 else None)


HEAD = "b" * 40
RELEASED = "a" * 40


class FakeGitHub(FakeDispatch):
    """FakeDispatch plus the reads of the release check: the branch head, the Release tag's object (a commit,
    or an annotated tag one hop away) and the compare between the two."""

    def __init__(
        self,
        head: str = RELEASED,
        message: str = "Update package versions",
        released: str = RELEASED,
        annotated: bool = False,
        files: list[str] | None = None,
        status: int = 204,
        fail: str | None = None,
    ) -> None:
        super().__init__(status)
        self.head, self.message, self.released, self.annotated = head, message, released, annotated
        self.files = files or []
        self.fail = fail
        self.gets: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method != "GET":
            return super().__call__(request)
        path = request.url.path
        self.gets.append(path)
        assert request.headers["Authorization"] == "Bearer ghs_token"
        if self.fail and self.fail in path:
            return httpx.Response(500, json={"message": "boom"})
        if "/branches/" in path:
            commit = {"sha": self.head, "commit": {"message": self.message}}
            return httpx.Response(200, json={"name": path.rsplit("/", 1)[-1], "commit": commit})
        if "/git/ref/tags/" in path:
            kind, sha = ("tag", "c" * 40) if self.annotated else ("commit", self.released)
            return httpx.Response(200, json={"ref": path.split("/git/ref/")[-1], "object": {"type": kind, "sha": sha}})
        if "/git/tags/" in path:
            return httpx.Response(200, json={"tag": "x", "object": {"type": "commit", "sha": self.released}})
        if "/compare/" in path:
            assert path.endswith(f"/compare/{self.released}...{self.head}")
            return httpx.Response(200, json={"status": "ahead", "files": [{"filename": f} for f in self.files]})
        return httpx.Response(404, json={"message": f"unexpected {path}"})


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


def live(
    state: State,
    branch: str = "main",
    data: dict[str, Any] | None = None,
    repo: str = REPO,
    seen: datetime.datetime | None = None,
) -> Any:
    """A repo row and a manifests row, as discovery would leave them; `seen` is when discovery first stored it."""
    state.model.upsert_repo(
        Repo.from_github({**fixture("api.repos")[0], "full_name": repo, "name": repo.split("/")[-1]})
    )
    manifest = data or MANIFEST
    if branch != "main":
        manifest = {**manifest, "release": {"branch": branch, "tag": branch, "channel": "prerelease"}}
    row = manifest_row(repo, branch, f"sha-{branch}", manifest, seen.isoformat(timespec="seconds") if seen else None)
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


async def run_release(
    state: State, manifest: Any, github: FakeGitHub | None = None, **kwargs: Any
) -> tuple[scheduler.Decision, FakeGitHub]:
    github = github or FakeGitHub()
    async with mock_client(github) as http:
        state.discord.client = http
        sched = scheduler.Scheduler(state, GitHub(http, "ghs_token"), **kwargs)
        return await sched.check_release(manifest), github


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
    assert len(github.discord) == 1
    (alert,) = github.discord
    assert alert["title"] == "sonarr main \u00b7 \U0001f534 upstream check failing" and alert["color"] == events.RED
    assert "failed 6 times in a row" in alert["description"] and "`ResolveError: HTTP 500`" in alert["description"]
    assert alert["url"] == f"{state.settings.site_url}/fleet/scheduler/?image=sonarr"
    assert f"[control]({state.settings.public_url}/scheduler?image=sonarr)" in alert["fields"][0]["value"]
    # A success resets the streak.
    decision, _ = await run_check(state, manifest, FakeResolver("1.0"))
    assert decision.outcome == "dispatch" and state.model.consecutive_errors("sonarr", "main") == 0


def verbose(state: State, monkeypatch: pytest.MonkeyPatch, on: bool = True) -> None:
    monkeypatch.setattr(state.discord, "webhook_url", "https://discord.com/api/webhooks/1/t")
    monkeypatch.setattr(state, "settings", state.settings.model_copy(update={"discord_verbose": on}))


@pytest.mark.anyio
async def test_quiet_embeds_only_when_verbose(state: State, monkeypatch: pytest.MonkeyPatch) -> None:
    seed(state, "release.published")  # 4.0.20.3014-ls1
    manifest = live(state)
    site = f"{state.settings.site_url}/fleet/scheduler/?image=sonarr"
    # Default: a rebuild dispatch and a skip-flag match say nothing on Discord.
    verbose(state, monkeypatch, on=False)
    decision, github = await run_release(state, manifest, FakeGitHub(head=HEAD), clock=lambda: NOW)
    assert decision.outcome == "dispatch" and github.discord == []
    state.model.set_skip("sonarr", "broken upstream", "octocat")
    decision, dispatch = await run_check(state, manifest, FakeResolver("9.9"), clock=lambda: NOW)
    assert decision.outcome == "skipped" and dispatch.discord == []

    # Verbose: one grey embed each, pointing at the branch's scheduler page on the website. (Clearing the flag
    # and checking once in between ends the skip streak the quiet phase started.)
    state.model.remove_skip("sonarr")
    decision, _ = await run_check(state, manifest, FakeResolver("9.9"), clock=lambda: NOW)
    assert decision.detail["reason"] == "cooldown"
    state.model.set_skip("sonarr", "broken upstream", "octocat")
    verbose(state, monkeypatch)
    decision, dispatch = await run_check(state, manifest, FakeResolver("9.9"), clock=lambda: NOW)
    assert decision.outcome == "skipped" and len(dispatch.discord) == 1
    (embed,) = dispatch.discord
    assert embed["title"] == "sonarr main \u00b7 \u26aa skipped by flag" and embed["color"] == events.GREY
    assert embed["description"] == "Held back by the skip flag `sonarr`: broken upstream" and embed["url"] == site
    # The flag matches every hourly check from now on; Discord hears about it once per streak.
    decision, dispatch = await run_check(state, manifest, FakeResolver("9.9"), clock=lambda: NOW)
    assert decision.outcome == "skipped" and dispatch.discord == []
    state.model.remove_skip("sonarr")

    later = NOW + scheduler.COOLDOWN
    decision, github = await run_release(state, manifest, FakeGitHub(head=HEAD), clock=lambda: later)
    assert decision.outcome == "dispatch" and len(github.discord) == 1
    (embed,) = github.discord
    assert embed["title"] == "sonarr main \u00b7 \u26aa rebuild dispatched" and embed["color"] == events.GREY
    assert embed["description"] == (
        f"Branch head `{HEAD[:7]}` is not the released commit `{RELEASED[:7]}` (4.0.20.3014-ls1); "
        "a rebuild was dispatched."
    )
    assert embed["url"] == site and embed["footer"]["text"] == "scheduler \u00b7 sonarr/main"
    # Other outcomes stay quiet even when verbose, and a dry run never posts.
    decision, github = await run_release(state, manifest, FakeGitHub(head=HEAD), clock=lambda: later)
    assert decision.outcome == "skipped" and decision.detail["reason"] == "cooldown" and github.discord == []
    much_later = later + scheduler.COOLDOWN
    decision, github = await run_release(state, manifest, FakeGitHub(head=HEAD), clock=lambda: much_later, dry_run=True)
    assert decision.outcome == "dispatch" and github.discord == []


@pytest.mark.anyio
async def test_dispatch_failure_is_an_error_decision(state: State) -> None:
    manifest = live(state)
    decision, _ = await run_check(state, manifest, FakeResolver("1.0"), FakeDispatch(status=422))
    assert decision.outcome == "error" and "422" in decision.detail["message"]
    assert decision.detail["upstream"] == "1.0"


# -- release check ------------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_release_check_rebuilds_an_unreleased_head(state: State) -> None:
    seed(state, "release.published")  # 4.0.20.3014-ls1
    manifest = live(state)
    decision, github = await run_release(state, manifest, FakeGitHub(head=HEAD), clock=lambda: NOW)
    assert decision.outcome == "dispatch" and decision.kind == "release"
    assert decision.detail["head"] == HEAD and decision.detail["released"] == RELEASED
    assert decision.detail["released_tag"] == "4.0.20.3014-ls1" and decision.detail["reason"] == "head not released"
    assert github.posts == [
        (f"/repos/{REPO}/actions/workflows/build.yml/dispatches", {"ref": "main", "inputs": {"reason": "rebuild"}})
    ]
    assert github.gets == [f"/repos/{REPO}/branches/main", f"/repos/{REPO}/git/ref/tags/4.0.20.3014-ls1"]
    # A plain commit is owed a release even when it only touched generated files: no compare is made.
    assert not any("/compare/" in g for g in github.gets)
    # The dispatch puts the branch in cooldown, for this check and the upstream one alike.
    again, github = await run_release(state, manifest, FakeGitHub(head=HEAD), clock=lambda: NOW)
    assert again.outcome == "skipped" and again.detail["reason"] == "cooldown" and github.posts == []
    upstream, _ = await run_check(state, manifest, FakeResolver("9.9"), clock=lambda: NOW)
    assert upstream.outcome == "skipped" and upstream.detail["reason"] == "cooldown"


@pytest.mark.anyio
async def test_release_check_unchanged_when_head_is_released(state: State) -> None:
    seed(state, "release.published")
    manifest = live(state)
    decision, github = await run_release(state, manifest, FakeGitHub(annotated=True), clock=lambda: NOW)
    assert decision.outcome == "unchanged" and github.posts == []
    assert decision.detail == {"head": RELEASED, "released": RELEASED, "released_tag": "4.0.20.3014-ls1"}
    # An annotated tag is one object further than the ref.
    assert github.gets[-1] == f"/repos/{REPO}/git/tags/{'c' * 40}"
    assert [(d.kind, d.outcome) for d in decisions(state)] == [("release", "unchanged")]


@pytest.mark.anyio
async def test_release_check_ignores_generated_files_behind_skip_ci(state: State) -> None:
    seed(state, "release.published")
    manifest = live(state)
    generated = ["README.md", "AGENTS.md", "LICENSE", ".github/workflows/build.yml"]
    github = FakeGitHub(head=HEAD, message="Update README [skip ci]\n\nregenerated", files=generated)
    decision, github = await run_release(state, manifest, github, clock=lambda: NOW)
    assert decision.outcome == "unchanged" and decision.detail["reason"] == "generated files only"
    assert decision.detail["head"] == HEAD and decision.detail["released"] == RELEASED and github.posts == []
    assert github.gets[-1] == f"/repos/{REPO}/compare/{RELEASED}...{HEAD}"
    # One real file among them and the release is owed, [skip ci] or not.
    github = FakeGitHub(
        head=HEAD, message="Update package versions [skip ci]", files=[*generated, "package_versions.txt"]
    )
    decision, github = await run_release(state, manifest, github, clock=lambda: NOW)
    assert decision.outcome == "dispatch" and len(github.posts) == 1
    assert decision.detail["dispatch_reason"] == "rebuild"
    # Only the subject counts: a body mentioning [skip ci] is a normal commit.
    assert scheduler.skips_ci("Fix build [skip ci]") and scheduler.skips_ci("Fix build [skip ci]  \nbody")
    assert not scheduler.skips_ci("Fix build\n\n[skip ci]") and not scheduler.skips_ci("[skip ci] Fix build")


@pytest.mark.anyio
async def test_release_check_builds_a_branch_without_any_release(state: State) -> None:
    """No Release means nothing to compare against: an initial build is dispatched (os images and pushes
    that produced no run depend on this); the cooldown then holds further dispatches."""
    manifest = live(state)
    decision, github = await run_release(state, manifest, FakeGitHub(head=HEAD), clock=lambda: NOW)
    assert decision.outcome == "dispatch" and decision.detail["reason"] == "no release yet, initial build"
    assert github.gets == []  # nothing to compare against, so no GitHub reads before the dispatch
    again, _ = await run_release(state, manifest, FakeGitHub(head=HEAD), clock=lambda: NOW)
    assert again.outcome == "skipped" and again.detail["reason"] == "cooldown"


@pytest.mark.anyio
async def test_a_failed_initial_build_is_retried_daily_not_every_cooldown(state: State) -> None:
    """An image broken upstream, or one a maintainer cancelled, is not rebuilt every two hours."""
    from control import handlers

    manifest = live(state)
    failed = fixture("workflow_run.completed")
    failed["workflow_run"].update(conclusion="failure", head_sha=HEAD)  # run 5001, created 12:00
    handlers.process(state.model, state.stream, "workflow_run", failed)
    decision, github = await run_release(state, manifest, FakeGitHub(head=HEAD), clock=lambda: NOW)
    assert decision.outcome == "skipped" and decision.detail["reason"] == "initial build did not pass, retried daily"
    assert decision.detail["run_id"] == 5001 and github.posts == []
    # A newer commit that got no run is built at once.
    decision, _ = await run_release(state, manifest, FakeGitHub(head="c" * 40), clock=lambda: NOW)
    assert decision.outcome == "dispatch"
    # The same head a day after the failed build is tried again.
    later = NOW + datetime.timedelta(hours=24)
    decision, _ = await run_release(state, manifest, FakeGitHub(head=HEAD), clock=lambda: later)
    assert decision.outcome == "dispatch" and decision.detail["reason"] == "no release yet, initial build"


@pytest.mark.anyio
async def test_release_check_waits_and_skips(state: State) -> None:
    manifest = live(state)
    seed(state, "release.published")

    # A queued or running build of the branch; then one that finished less than 15 minutes ago.
    seed(state, "workflow_run.requested")  # run 5001 on main, created 12:00
    decision, _ = await run_release(state, manifest, FakeGitHub(head=HEAD), clock=lambda: NOW)
    assert decision.outcome == "skipped" and decision.detail["reason"] == "run in progress"
    seed(state, "workflow_run.completed")
    young = NOW.replace(hour=12, minute=10)
    decision, github = await run_release(state, manifest, FakeGitHub(head=HEAD), clock=lambda: young)
    assert (
        decision.outcome == "skipped"
        and decision.detail["reason"] == "recent run"
        and decision.detail["run_id"] == 5001
    )
    assert github.gets == []
    settled = NOW.replace(hour=12, minute=15)
    decision, _ = await run_release(state, manifest, FakeGitHub(head=HEAD), clock=lambda: settled, dry_run=True)
    assert decision.outcome == "dispatch" and decision.detail["dry_run"] is True

    # Skip flags hold it back like every dispatch.
    state.model.set_skip("sonarr/main", "hold", "octocat")
    decision, _ = await run_release(state, manifest, FakeGitHub(head=HEAD), clock=lambda: NOW)
    assert decision.outcome == "skipped" and decision.detail["scope"] == "sonarr/main"
    state.model.remove_skip("sonarr/main")

    # A failing read is an error decision, never an exception out of the tick.
    decision, _ = await run_release(state, manifest, FakeGitHub(head=HEAD, fail="/git/ref/"), clock=lambda: NOW)
    assert decision.outcome == "error" and "500" in decision.detail["message"]
    assert decision.detail["released_tag"] == "4.0.20.3014-ls1"
    # Without the App there is nothing to read with.
    bare = scheduler.Scheduler(state, None, clock=lambda: NOW)
    decision = await bare.check_release(manifest)
    assert decision.outcome == "error" and "not configured" in decision.detail["reason"]


# -- package check ------------------------------------------------------------------------------------------------


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
    manifest = live(state, seen=NOW - datetime.timedelta(weeks=2))  # long enough known for the weekly slot
    seed(state, "release.published")
    state.db.set("last_discovery", json.dumps({"started": NOW.isoformat()}))  # discovery not due
    github = FakeGitHub()  # head is the released commit
    resolver = FakeResolver("4.0.20.3014")
    now = NOW
    async with mock_client(github) as http:
        sched = scheduler.Scheduler(state, GitHub(http, "ghs_token"), resolver=resolver, clock=lambda: now)
        first = await sched.tick()
        kinds = [d.kind for d in first]
        assert kinds == ["upstream", "release", "packages"]  # nothing recorded before: every schedule is due
        assert [d.outcome for d in first] == ["unchanged", "unchanged", "dispatch"]
        now = NOW + datetime.timedelta(minutes=1)
        assert await sched.tick() == []  # same slot, already done
        now = NOW + datetime.timedelta(hours=1, minutes=1)
        second = await sched.tick()
        assert [d.kind for d in second] == ["upstream", "release"]  # a new hour, same week
        assert {(d.outcome, d.detail["reason"]) for d in second} == {("skipped", "cooldown")}  # the package dispatch
        # A manifest that opts out of the package check only gets the hourly schedules.
        state.model.upsert_manifest(manifest.model_copy(update={"package_check": False}))
        state.model.upsert_manifest(live(state, "develop").model_copy(update={"package_check": False}))
        now = NOW + datetime.timedelta(weeks=1, hours=2)
        third = await sched.tick()
        assert sorted((d.branch, d.kind) for d in third) == [
            ("develop", "release"),
            ("develop", "upstream"),
            ("main", "release"),
            ("main", "upstream"),
        ]
    # One package dispatch for main, one upstream dispatch for develop (which has no release yet); main unchanged.
    assert len(resolver.calls) == 3
    assert [(p[1]["ref"], p[1]["inputs"]["reason"]) for p in github.posts] == [
        ("main", "packages"),
        ("develop", "upstream"),
    ]


@pytest.mark.anyio
async def test_first_package_check_waits_for_the_branch_slot(state: State) -> None:
    """A branch nobody checked yet is due at its weekly slot, counted from when discovery stored it, not at
    the first tick: on a fresh start the fleet's package checks spread over the week like every later one."""
    seen = NOW - datetime.timedelta(minutes=5)
    manifest = live(state, seen=seen)
    state.db.set("last_discovery", json.dumps({"started": NOW.isoformat()}))
    github = FakeGitHub()
    resolver = FakeResolver("1.0")
    now = NOW
    async with mock_client(github) as http:
        sched = scheduler.Scheduler(state, GitHub(http, "ghs_token"), resolver=resolver, clock=lambda: now)
        first = await sched.tick()
        assert [d.kind for d in first] == ["upstream", "release"]  # the hourly checks, no package check
        nxt = scheduler.next_package_check(REPO, "main", seen)
        assert seen < nxt <= seen + datetime.timedelta(weeks=1)
        now = nxt - datetime.timedelta(seconds=1)
        assert all(d.kind != "packages" for d in await sched.tick())
        now = nxt
        fired = [d for d in await sched.tick() if d.kind == "packages"]
        assert len(fired) == 1 and fired[0].outcome == "dispatch" and fired[0].branch == "main"
        now = nxt + datetime.timedelta(minutes=1)
        assert all(d.kind != "packages" for d in await sched.tick())  # recorded: not again this week
        # The upcoming list named that same slot all along.
        row = next(
            r for r in scheduler.upcoming([manifest], seen, datetime.timedelta(weeks=1)) if r["kind"] == "packages"
        )
        assert row["at"] == nxt.isoformat(timespec="seconds")
    assert [p[1]["inputs"]["reason"] for p in github.posts if p[1]["inputs"]["reason"] == "packages"] == ["packages"]


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
    # A forced package check is recorded as one, so it resets the branch's weekly slot.
    forced = await scheduler.run_once(state, image="sonarr", branch="main", dry_run=True, kind="packages")
    assert [(d.kind, d.outcome) for d in forced] == [("packages", "dispatch")]
    assert forced[0].detail["dispatch_reason"] == "packages"
    released = await scheduler.run_once(state, branch="main", kind="release")
    # No Release yet means an initial build; without an installation that dispatch is recorded as an error.
    assert [(d.kind, d.outcome) for d in released] == [("release", "error")]
    assert released[0].detail["reason"] == "no release yet, initial build"


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
    assert (
        main(["schedule", "--once", "--image", "docker-sonarr", "--branch", "main", "--kind", "packages", "--dry-run"])
        == 0
    )
    assert capsys.readouterr().out.startswith("would sonarr/main [packages] dispatch: reason=weekly package check")
    assert state.model.recent_decisions() == []
    with pytest.raises(SystemExit):
        main(["schedule"])  # --once is required
    with pytest.raises(SystemExit):
        main(["schedule", "--once", "--kind", "packages"])  # a package check for the whole fleet needs --image


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
