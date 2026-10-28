"""The run archive: a released run's run.json and logs.zip land on its Release once, and credentials never do."""

import asyncio
import datetime
import io
import json
import zipfile
from typing import Any

import httpx
import pytest

from control import archive
from control.github_app import AppCredentials
from control.state import State

from .conftest import EVENTS_SECRET, FINISHED_SEED, REPO, deliver, fixture, install, mock_discord, seed, token_response

TAG = "4.0.20.3014-ls1"
UPLOADS = f"https://uploads.github.com/repos/{REPO}/releases/42/assets"
BLOB = "https://results-receiver.example/logs/5001.zip?sig=abc"


def logs_zip(*lines: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive_file:
        archive_file.writestr("0_build (amd64).txt", "2026-10-24T12:00:00Z ##[group]Run docker build\nok\n")
        archive_file.writestr("build (amd64)/3_Build.txt", "\n".join(lines) + "\n")
    return buffer.getvalue()


class Clock:
    def __init__(self) -> None:
        self.at = datetime.datetime.now(datetime.UTC)

    def __call__(self) -> str:
        return self.at.isoformat(timespec="seconds")

    def advance(self, **delta: float) -> None:
        self.at += datetime.timedelta(**delta)


class GitHubWorld:
    """The release, the run, its jobs, its logs (behind a redirect) and the upload endpoint, plus Discord."""

    def __init__(self) -> None:
        self.assets: list[str] = []
        self.logs: bytes | int = logs_zip("all good, token ***")
        self.release_exists = True
        self.uploads: list[tuple[str, str, bytes]] = []
        self.discord: list[str] = []
        self.blob_auth: list[str | None] = []
        self.requests: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if (token := token_response(request)) is not None:
            return token
        url = str(request.url)
        path = request.url.path
        self.requests.append(f"{request.method} {path}")
        if "discord.com" in url:
            self.discord.append(json.loads(request.content).get("content", ""))
            return httpx.Response(200, json={"id": "msg-1"})
        if url.startswith(BLOB):
            self.blob_auth.append(request.headers.get("Authorization"))
            assert isinstance(self.logs, bytes)
            return httpx.Response(200, content=self.logs)
        if path == f"/repos/{REPO}/releases/tags/{TAG}":
            if not self.release_exists:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(
                200,
                json={
                    "id": 42,
                    "tag_name": TAG,
                    "upload_url": f"{UPLOADS}{{?name,label}}",
                    "assets": [{"name": name} for name in self.assets],
                },
            )
        if path == f"/repos/{REPO}/actions/runs/5001":
            return httpx.Response(200, json=fixture("workflow_run.completed")["workflow_run"])
        if path == f"/repos/{REPO}/actions/runs/5001/jobs":
            return httpx.Response(200, json=fixture("api.jobs"))
        if path == f"/repos/{REPO}/actions/runs/5001/logs":
            if isinstance(self.logs, int):
                return httpx.Response(self.logs, json={"message": "no logs"})
            return httpx.Response(302, headers={"Location": BLOB})
        if url.startswith(UPLOADS) and request.method == "POST":
            name = request.url.params["name"]
            self.uploads.append((name, request.headers["Content-Type"], request.content))
            self.assets.append(name)
            return httpx.Response(201, json={"name": name})
        return httpx.Response(404, json={"message": f"no fixture for {path}"})

    def uploaded(self) -> list[str]:
        return [name for name, _, _ in self.uploads]


@pytest.fixture
def world(state: State, credentials: AppCredentials, monkeypatch: pytest.MonkeyPatch) -> GitHubWorld:
    world = GitHubWorld()
    install(state, credentials)
    mock_discord(state, monkeypatch, world)
    seed(state, *FINISHED_SEED)
    return world


def test_a_released_run_gets_its_record_and_logs_once(state: State, world: GitHubWorld) -> None:
    clock = Clock()
    assert asyncio.run(archive.archive_once(state, clock)) == 1
    assert world.uploaded() == ["run.json", "logs.zip"]
    (_, json_type, body), (_, zip_type, logs) = world.uploads
    assert json_type == "application/json" and zip_type == "application/zip"
    assert logs == world.logs
    # The signed storage URL never sees our token.
    assert world.blob_auth == [None]
    record = json.loads(body)
    assert record["schema"] == 1 and record["release"]["tag"] == TAG
    assert record["run"]["id"] == 5001 and record["run"]["path"] == ".github/workflows/build.yml"
    assert record["trigger"]["reason"] == "upstream" and record["trigger"]["event"]
    assert record["build"]["image"] == "sonarr" and record["build"]["mode"] == "live"
    job = record["jobs"][0]
    assert {"name", "runner_name", "labels", "started_at", "completed_at", "conclusion", "steps"} <= set(job)
    assert job["steps"] and set(job["steps"][0]) == {
        "number",
        "name",
        "status",
        "conclusion",
        "started_at",
        "completed_at",
    }
    row = state.model.run_archive(REPO, 5001)
    assert row and row.state == "done" and row.run_json == "uploaded" and row.logs == "uploaded"
    # Done means done: the next sweep has nothing to do and asks GitHub nothing.
    before = len(world.requests)
    assert asyncio.run(archive.archive_once(state, clock)) == 0
    assert len(world.requests) == before


def test_assets_already_on_the_release_are_left_alone(state: State, world: GitHubWorld) -> None:
    world.assets = ["build.json", "run.json", "logs.zip"]
    asyncio.run(archive.archive_once(state, Clock()))
    assert world.uploads == []
    row = state.model.run_archive(REPO, 5001)
    assert row and row.state == "done" and row.run_json == "present" and row.logs == "present"
    assert f"GET /repos/{REPO}/actions/runs/5001/logs" not in world.requests


@pytest.mark.parametrize(
    "line",
    [
        "token ghp_" + "a" * 36,
        "export GH=ghs_" + "B1" * 18,
        "pat github_pat_" + "x" * 30,
        "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJyZXBvOmV4YW1wbGUifQ.c2lnbmF0dXJlLXZhbHVl",
        "> Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345",
        "posting to https://discord.com/api/webhooks/123456789/abcdefghijklmnopqrstuvwxyz",
        f"oops {EVENTS_SECRET}-and-more",
    ],
)
def test_credential_shaped_logs_are_withheld_and_reported_without_the_match(
    state: State, world: GitHubWorld, line: str
) -> None:
    world.logs = logs_zip("starting", line, "done")
    assert archive.own_secrets(state) == (EVENTS_SECRET,)
    asyncio.run(archive.archive_once(state, Clock()))
    assert world.uploaded() == ["run.json"]
    row = state.model.run_archive(REPO, 5001)
    assert row and row.state == "done" and row.logs == "withheld"
    assert row.note and "build (amd64)/3_Build.txt" in row.note
    assert len(world.discord) == 1 and "logs.zip" in world.discord[0]
    secret_part = line.split()[-1]
    assert secret_part not in world.discord[0] and secret_part not in (row.note or "")


def test_masked_and_unexpanded_credentials_are_not_matches() -> None:
    content = logs_zip(
        "  AUTHORIZATION: basic ***",
        "curl -H 'Authorization: Bearer $GITHUB_TOKEN' https://api.github.com",
        "  Authorization: token ${{ secrets.PACKAGES_TOKEN }}",
        "ATTACHMENTS_TOKEN: ***",
        "ghp_short",
        "https://discord.com/api/webhooks/***",
    )
    assert archive.scan_logs(content, ("a-real-secret-value",)) == []
    with pytest.raises(ValueError):
        archive.scan_logs(b"not a zip")


def test_logs_not_ready_are_retried_with_backoff_and_run_json_is_not_sent_twice(
    state: State, world: GitHubWorld
) -> None:
    clock = Clock()
    world.logs = 404
    asyncio.run(archive.archive_once(state, clock))
    assert world.uploaded() == ["run.json"]
    row = state.model.run_archive(REPO, 5001)
    assert row and row.state == "retry" and row.attempts == 1 and row.next_attempt_at
    # Not due yet.
    assert asyncio.run(archive.archive_once(state, clock)) == 0
    clock.advance(minutes=6)
    world.logs = logs_zip("fine")
    assert asyncio.run(archive.archive_once(state, clock)) == 1
    assert world.uploaded() == ["run.json", "logs.zip"]
    row = state.model.run_archive(REPO, 5001)
    assert row and row.state == "done" and row.logs == "uploaded"


def test_expired_logs_still_leave_the_record(state: State, world: GitHubWorld) -> None:
    world.logs = 410
    asyncio.run(archive.archive_once(state, Clock()))
    row = state.model.run_archive(REPO, 5001)
    assert world.uploaded() == ["run.json"] and row and row.state == "done" and row.logs == "expired"


def test_control_gives_up_after_the_configured_attempts_and_says_so(
    state: State, world: GitHubWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(state, "settings", state.settings.model_copy(update={"run_archive_attempts": 2}))
    clock = Clock()
    world.logs = 503
    asyncio.run(archive.archive_once(state, clock))
    clock.advance(hours=7)
    asyncio.run(archive.archive_once(state, clock))
    row = state.model.run_archive(REPO, 5001)
    assert row and row.state == "failed" and row.attempts == 2
    assert len(world.discord) == 1 and "after 2 attempts" in world.discord[0]
    clock.advance(days=1)
    assert asyncio.run(archive.archive_once(state, clock)) == 0


def test_a_missing_release_is_skipped(state: State, world: GitHubWorld) -> None:
    world.release_exists = False
    asyncio.run(archive.archive_once(state, Clock()))
    row = state.model.run_archive(REPO, 5001)
    assert row and row.state == "skipped" and world.uploads == []


def test_only_released_completed_runs_are_candidates(state: State) -> None:
    now = datetime.datetime.now(datetime.UTC)
    later = (now + datetime.timedelta(minutes=5)).isoformat(timespec="seconds")
    settled = (now - datetime.timedelta(hours=1)).isoformat(timespec="seconds")
    # Released but still running: not yet, unless its completion webhook is long overdue.
    seed(state, "workflow_run.in_progress", "control.build.summary")
    assert state.model.archive_candidates(later, settled, 10) == []
    assert state.model.archive_candidates(later, later, 10) == [(REPO, 5001)]
    seed(state, "workflow_run.completed")
    assert state.model.archive_candidates(later, settled, 10) == [(REPO, 5001)]
    # A failed live build released nothing.
    seed(state, "control.build.summary.failed")
    assert state.model.archive_candidates(later, later, 10) == []


def test_a_completed_run_wakes_the_archiver(state: State, credentials: AppCredentials, client: Any) -> None:
    state.credentials.save(credentials)
    assert not state.archive_wakeup.is_set()
    payload = fixture("workflow_run.completed")
    assert deliver(client, credentials.webhook_secret, "workflow_run", payload).status_code == 202
    assert state.archive_wakeup.is_set()


def test_logs_missing_past_retention_count_as_expired(state: State, world: GitHubWorld) -> None:
    clock = Clock()
    clock.advance(days=100)
    world.logs = 404
    asyncio.run(archive.archive_once(state, clock))
    row = state.model.run_archive(REPO, 5001)
    assert row and row.state == "done" and row.logs == "expired" and world.uploaded() == ["run.json"]


def given_up(state: State, n: int, note: str, at: str, logs: str | None = None) -> None:
    """A run_archives row as `failed` (or `done` with `logs`) leaves it, for docker-app<n>."""
    repo = f"example-org/docker-app{n}"
    row = archive._new_row(repo, 6000 + n, f"1.{n}-ls1", at)
    final = {"state": "done", "logs": logs} if logs else {"state": "failed", "attempts": 8}
    state.model.upsert_run_archive(row.model_copy(update={**final, "note": note, "updated_at": at}))


class Discord:
    def __init__(self, status: int = 200) -> None:
        self.posts: list[str] = []
        self.status = status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.posts.append(json.loads(request.content).get("content", ""))
        return httpx.Response(self.status, json={"id": "msg-1"})


def test_give_ups_are_one_summary_an_hour(state: State, monkeypatch: pytest.MonkeyPatch) -> None:
    discord = Discord()
    mock_discord(state, monkeypatch, discord)
    clock = Clock()
    for n in range(5):
        given_up(state, n, "GitHub answered 403", clock())
    for n in range(5, 7):
        given_up(state, n, "ReadTimeout", clock())
    assert asyncio.run(archive.report(state, clock)) is True
    assert len(discord.posts) == 1
    post = discord.posts[0]
    assert "gave up archiving 7 runs on their releases after 8 attempts" in post
    assert "most common reason: GitHub answered 403" in post
    assert "docker-app0 1.0-ls1" in post and "docker-app4 1.4-ls1" in post and "docker-app5" not in post
    assert "and 2 more" in post and "/actions/runs/6000" in post
    assert state.model.unreported_run_archives() == []
    # More give-ups within the hour wait for it, then go out together.
    clock.advance(minutes=10)
    given_up(state, 7, "GitHub answered 403", clock())
    given_up(state, 8, "GitHub answered 403", clock())
    assert asyncio.run(archive.report(state, clock)) is False
    clock.advance(minutes=51)
    assert asyncio.run(archive.report(state, clock)) is True
    assert len(discord.posts) == 2 and "gave up archiving 2 runs" in discord.posts[1]
    # Nothing new, nothing posted.
    clock.advance(hours=2)
    assert asyncio.run(archive.report(state, clock)) is False and len(discord.posts) == 2


def test_withheld_logs_are_grouped_by_their_most_common_match(state: State, monkeypatch: pytest.MonkeyPatch) -> None:
    discord = Discord()
    mock_discord(state, monkeypatch, discord)
    clock = Clock()
    token = archive.describe([("a GitHub token", "build (amd64)/3_Build.txt")])
    both = archive.describe([("a GitHub token", "a.txt"), ("a JWT", "b.txt")])
    given_up(state, 1, token, clock(), logs="withheld")
    given_up(state, 2, both, clock(), logs="withheld")
    given_up(state, 3, "GitHub answered 403", clock())
    # Done runs whose logs went up, or expired, are no news.
    given_up(state, 4, "", clock(), logs="uploaded")
    assert archive.matched_kinds(both) == ["a GitHub token", "a JWT"]
    assert asyncio.run(archive.report(state, clock)) is True
    (post,) = discord.posts
    assert "gave up archiving 1 run on its release" in post
    assert "the logs of 2 runs matched a credential pattern" in post and "most common match: a GitHub token" in post
    assert "docker-app4" not in post


def test_a_summary_discord_refuses_is_retried_after_the_hour(state: State, monkeypatch: pytest.MonkeyPatch) -> None:
    discord = Discord(status=500)
    mock_discord(state, monkeypatch, discord)
    clock = Clock()
    given_up(state, 1, "GitHub answered 403", clock())
    assert asyncio.run(archive.report(state, clock)) is False and len(discord.posts) == 1
    assert len(state.model.unreported_run_archives()) == 1
    clock.advance(minutes=30)
    assert asyncio.run(archive.report(state, clock)) is False and len(discord.posts) == 1
    discord.status = 200
    clock.advance(minutes=31)
    assert asyncio.run(archive.report(state, clock)) is True
    assert state.model.unreported_run_archives() == []


def test_a_rewritten_row_keeps_its_report(state: State, monkeypatch: pytest.MonkeyPatch) -> None:
    mock_discord(state, monkeypatch, Discord())
    clock = Clock()
    given_up(state, 1, "GitHub answered 403", clock())
    stale = state.model.run_archive("example-org/docker-app1", 6001)
    assert stale and asyncio.run(archive.report(state, clock)) is True
    state.model.upsert_run_archive(stale)
    row = state.model.run_archive("example-org/docker-app1", 6001)
    assert row and row.reported_at == clock()
